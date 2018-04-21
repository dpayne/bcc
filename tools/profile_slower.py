#!/usr/bin/python
# @lint-avoid-python-3-compatibility-imports
#
# profile  Profile CPU usage by sampling stack traces at a timed interval.
#          For Linux, uses BCC, BPF, perf_events. Embedded C.
#
# This is an efficient profiler, as stack traces are frequency counted in
# kernel context, rather than passing every stack to user space for frequency
# counting there. Only the unique stacks and counts are passed to user space
# at the end of the profile, greatly reducing the kernel<->user transfer.
#
# This uses perf_event_open to setup a timer which is instrumented by BPF,
# and for efficiency it does not initialize the perf ring buffer, so the
# redundant perf samples are not collected.
#
# REQUIRES: Linux 4.9+ (BPF_PROG_TYPE_PERF_EVENT support). Under tools/old is
# a version of this tool that may work on Linux 4.6 - 4.8.
#
# Copyright 2016 Netflix, Inc.
# Licensed under the Apache License, Version 2.0 (the "License")
#
# THANKS: Alexei Starovoitov, who added proper BPF profiling support to Linux;
# Sasha Goldshtein, Andrew Birchall, and Evgeny Vereshchagin, who wrote much
# of the code here, borrowed from tracepoint.py and offcputime.py; and
# Teng Qin, who added perf support in bcc.
#
# 15-Jul-2016   Brendan Gregg   Created this.
# 20-Oct-2016      "      "     Switched to use the new 4.9 support.

from __future__ import print_function
from bcc import BPF, PerfType, PerfSWConfig
from sys import stderr
from time import sleep
import argparse
import signal
import os
import errno
import multiprocessing
import ctypes as ct

#
# Process Arguments
#

# arg validation
def positive_int(val):
    try:
        ival = int(val)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer")

    if ival < 0:
        raise argparse.ArgumentTypeError("must be positive")
    return ival

def positive_nonzero_int(val):
    ival = positive_int(val)
    if ival == 0:
        raise argparse.ArgumentTypeError("must be nonzero")
    return ival

# arguments
examples = """examples:
    ./profile             # profile stack traces at 49 Hertz until Ctrl-C
    ./profile -F 99       # profile stack traces at 99 Hertz
    ./profile 5           # profile at 49 Hertz for 5 seconds only
    ./profile -f 5        # output in folded format for flame graphs
    ./profile -p 185      # only profile threads for PID 185
    ./profile -U          # only show user space stacks (no kernel)
    ./profile -K          # only show kernel space stacks (no user)
"""
parser = argparse.ArgumentParser(
    description="Profile CPU stack traces at a timed interval",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
thread_group = parser.add_mutually_exclusive_group()
thread_group.add_argument("-p", "--pid", type=positive_int,
    help="profile this PID only")
# TODO: add options for user/kernel threads only
stack_group = parser.add_mutually_exclusive_group()
stack_group.add_argument("-U", "--user-stacks-only", action="store_true",
    help="show stacks from user space only (no kernel space stacks)")
stack_group.add_argument("-K", "--kernel-stacks-only", action="store_true",
    help="show stacks from kernel space only (no user space stacks)")
parser.add_argument("-F", "--frequency", type=positive_int, default=49,
    help="sample frequency, Hertz (default 49)")
parser.add_argument("-d", "--delimited", action="store_true",
    help="insert delimiter between kernel/user stacks")
parser.add_argument("-v", "--verbose", action="store_true",
    help="print the BPF program for debugging purposes")
parser.add_argument("-a", "--annotations", action="store_true",
    help="add _[k] annotations to kernel frames")
parser.add_argument("-f", "--folded", action="store_true",
    help="output folded format, one line per stack (for flame graphs)")
parser.add_argument("--stack-storage-size", default=10240,
    type=positive_nonzero_int,
    help="the number of unique stack traces that can be stored and "
        "displayed (default 2048)")
parser.add_argument("-T", "--duration", default=99999999,
    type=positive_nonzero_int,
    help="duration of trace, in seconds")
parser.add_argument(metavar="function", nargs="+", dest="functions",
    help="function(s) to trace")
parser.add_argument("--ebpf", action="store_true",
    help=argparse.SUPPRESS)

# option logic
args = parser.parse_args()
pid = int(args.pid) if args.pid is not None else -1
duration = int(args.duration)
debug = 0
need_delimiter = args.delimited and not (args.kernel_stacks_only or
    args.user_stacks_only)
# TODO: add stack depth, and interval

#
# Setup BPF
#

# define BPF program
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <uapi/linux/bpf_perf_event.h>
#include <linux/sched.h>

struct entry_t {
    u64 id;
    u64 start_ns;
#ifdef GRAB_ARGS
    u64 args[6];
#endif
};

struct data_t {
    u64 id;
    u64 tgid_pid;
    u64 start_ns;
    u64 duration_ns;
    u64 retval;
    char comm[TASK_COMM_LEN];
#ifdef GRAB_ARGS
    u64 args[6];
#endif
};

struct key_t {
    u32 pid;
    u64 kernel_ip;
    u64 kernel_ret_ip;
    int user_stack_id;
    int kernel_stack_id;
    char name[TASK_COMM_LEN];
};

BPF_HASH(entryinfo, u64, struct entry_t);
BPF_HASH(counts, struct key_t);
BPF_STACK_TRACE(stack_traces, STACK_STORAGE_SIZE);

static int trace_entry(struct pt_regs *ctx, int id)
{
    u64 tgid_pid = bpf_get_current_pid_tgid();
    u32 tgid = tgid_pid >> 32;
    if (TGID_FILTER)
        return 0;

    u32 pid = tgid_pid;

    struct entry_t entry = {};
    entry.start_ns = bpf_ktime_get_ns();
    entry.id = id;
#ifdef GRAB_ARGS
    entry.args[0] = PT_REGS_PARM1(ctx);
    entry.args[1] = PT_REGS_PARM2(ctx);
    entry.args[2] = PT_REGS_PARM3(ctx);
    entry.args[3] = PT_REGS_PARM4(ctx);
    entry.args[4] = PT_REGS_PARM5(ctx);
    entry.args[5] = PT_REGS_PARM6(ctx);
#endif

    entryinfo.update(&tgid_pid, &entry);

    return 0;
}

int trace_return(struct pt_regs *ctx)
{
    struct entry_t *entryp;
    u64 tgid_pid = bpf_get_current_pid_tgid();

    entryp = entryinfo.lookup(&tgid_pid);
    if (entryp == 0) {
        return 0;
    }

    u64 delta_ns = bpf_ktime_get_ns() - entryp->start_ns;
    entryinfo.delete(&tgid_pid);

    return 0;
}

// This code gets a bit complex. Probably not suitable for casual hacking.

int do_perf_event(struct bpf_perf_event_data *ctx) {
    u32 pid = bpf_get_current_pid_tgid() >> 32;
    if (!(THREAD_FILTER))
        return 0;

    struct entry_t *entryp;
    u64 tgid_pid = bpf_get_current_pid_tgid();

    entryp = entryinfo.lookup(&tgid_pid);
    if (entryp == 0) {
        return 0;
    }

    u64 delta_ns = bpf_ktime_get_ns() - entryp->start_ns;

    if (delta_ns < DURATION_NS)
        return 0;

    // create map key
    u64 zero = 0, *val;
    struct key_t key = {.pid = pid};
    bpf_get_current_comm(&key.name, sizeof(key.name));

    // get stacks
    key.user_stack_id = USER_STACK_GET;
    key.kernel_stack_id = KERNEL_STACK_GET;

    if (key.kernel_stack_id >= 0) {
        // populate extras to fix the kernel stack
        struct pt_regs regs = {};
        bpf_probe_read(&regs, sizeof(regs), (void *)&ctx->regs);
        u64 ip = PT_REGS_IP(&regs);
        u64 page_offset;

        // if ip isn't sane, leave key ips as zero for later checking
#if defined(CONFIG_X86_64) && defined(__PAGE_OFFSET_BASE)
        // x64, 4.16, ..., 4.11, etc., but some earlier kernel didn't have it
        page_offset = __PAGE_OFFSET_BASE;
#elif defined(CONFIG_X86_64) && defined(__PAGE_OFFSET_BASE_L4)
        // x64, 4.17, and later
#if defined(CONFIG_DYNAMIC_MEMORY_LAYOUT) && defined(CONFIG_X86_5LEVEL)
        page_offset = __PAGE_OFFSET_BASE_L5;
#else
        page_offset = __PAGE_OFFSET_BASE_L4;
#endif
#else
        // earlier x86_64 kernels, e.g., 4.6, comes here
        // arm64, s390, powerpc, x86_32
        page_offset = PAGE_OFFSET;
#endif

        if (ip > page_offset) {
            key.kernel_ip = ip;
        }
    }

    val = counts.lookup_or_init(&key, &zero);
    (*val)++;
    return 0;
}
"""

# set thread filter
thread_context = ""
perf_filter = "-a"
if args.pid is not None:
    thread_context = "PID %s" % args.pid
    thread_filter = 'pid == %s' % args.pid
    perf_filter = '-p %s' % args.pid
else:
    thread_context = "all threads"
    thread_filter = '1'
bpf_text = bpf_text.replace('THREAD_FILTER', thread_filter)

# set stack storage size
bpf_text = bpf_text.replace('STACK_STORAGE_SIZE', str(args.stack_storage_size))

# handle stack args
kernel_stack_get = \
    "stack_traces.get_stackid(&ctx->regs, 0 | BPF_F_REUSE_STACKID)"
user_stack_get = \
    "stack_traces.get_stackid(&ctx->regs, 0 | BPF_F_REUSE_STACKID | " \
    "BPF_F_USER_STACK)"
stack_context = ""
if args.user_stacks_only:
    stack_context = "user"
    kernel_stack_get = "-1"
elif args.kernel_stacks_only:
    stack_context = "kernel"
    user_stack_get = "-1"
else:
    stack_context = "user + kernel"
bpf_text = bpf_text.replace('USER_STACK_GET', user_stack_get)
bpf_text = bpf_text.replace('KERNEL_STACK_GET', kernel_stack_get)


# TODO: make duration_ns and tgid parameters
duration_ns = 1000000   # default to 1ms
bpf_text = bpf_text.replace('DURATION_NS', str(duration_ns))
bpf_text = bpf_text.replace('TGID_FILTER', '0')

# header
if not args.folded:
    print("Sampling at %d Hertz of %s by %s stack" %
        (args.frequency, thread_context, stack_context), end="")
    if duration < 99999999:
        print(" for %d secs." % duration)
    else:
        print("... Hit Ctrl-C to end.")

for i in range(len(args.functions)):
    bpf_text += """
int trace_%d(struct pt_regs *ctx) {
    return trace_entry(ctx, %d);
}
""" % (i, i)


if debug or args.ebpf:
    print(bpf_text)
    if args.ebpf:
        exit()

# initialize BPF & perf_events
b = BPF(text=bpf_text)
b.attach_perf_event(ev_type=PerfType.SOFTWARE,
    ev_config=PerfSWConfig.CPU_CLOCK, fn_name="do_perf_event",
    sample_period=0, sample_freq=args.frequency)

for i, function in enumerate(args.functions):
    if ":" in function:
        library, func = function.split(":")
        b.attach_uprobe(name=library, sym=func, fn_name="trace_%d" % i)
        b.attach_uretprobe(name=library, sym=func, fn_name="trace_return")
    else:
        b.attach_kprobe(event=function, fn_name="trace_%d" % i)
        b.attach_kretprobe(event=function, fn_name="trace_return")


# signal handler
def signal_ignore(signal, frame):
    print()

#
# Output Report
#

# collect samples
try:
    sleep(duration)
except KeyboardInterrupt:
    # as cleanup can take some time, trap Ctrl-C:
    signal.signal(signal.SIGINT, signal_ignore)

if not args.folded:
    print()

def aksym(addr):
    if args.annotations:
        return b.ksym(addr) + "_[k]"
    else:
        return b.ksym(addr)

# output stacks
missing_stacks = 0
has_enomem = False
counts = b.get_table("counts")
stack_traces = b.get_table("stack_traces")
for k, v in sorted(counts.items(), key=lambda counts: counts[1].value):
    # handle get_stackid erorrs
    if (not args.user_stacks_only and k.kernel_stack_id < 0 and
            k.kernel_stack_id != -errno.EFAULT) or \
            (not args.kernel_stacks_only and k.user_stack_id < 0 and
            k.user_stack_id != -errno.EFAULT):
        missing_stacks += 1
        # check for an ENOMEM error
        if k.kernel_stack_id == -errno.ENOMEM or \
                k.user_stack_id == -errno.ENOMEM:
            has_enomem = True

    user_stack = [] if k.user_stack_id < 0 else \
        stack_traces.walk(k.user_stack_id)
    kernel_tmp = [] if k.kernel_stack_id < 0 else \
        stack_traces.walk(k.kernel_stack_id)

    # fix kernel stack
    kernel_stack = []
    if k.kernel_stack_id >= 0:
        for addr in kernel_tmp:
            kernel_stack.append(addr)
        # the later IP checking
        if k.kernel_ip:
            kernel_stack.insert(0, k.kernel_ip)

    do_delimiter = need_delimiter and kernel_stack

    if args.folded:
        # print folded stack output
        user_stack = list(user_stack)
        kernel_stack = list(kernel_stack)
        line = [k.name.decode()] + \
            [b.sym(addr, k.pid) for addr in reversed(user_stack)] + \
            (do_delimiter and ["-"] or []) + \
            [aksym(addr) for addr in reversed(kernel_stack)]
        print("%s %d" % (";".join(line), v.value))
    else:
        # print default multi-line stack output.
        for addr in kernel_stack:
            print("    %s" % aksym(addr))
        if do_delimiter:
            print("    --")
        for addr in user_stack:
            print("    %s" % b.sym(addr, k.pid))
        print("    %-16s %s (%d)" % ("-", k.name.decode(), k.pid))
        print("        %d\n" % v.value)

# check missing
if missing_stacks > 0:
    enomem_str = "" if not has_enomem else \
        " Consider increasing --stack-storage-size."
    print("WARNING: %d stack traces could not be displayed.%s" %
        (missing_stacks, enomem_str),
        file=stderr)
