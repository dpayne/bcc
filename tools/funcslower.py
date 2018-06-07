#!/usr/bin/python
# @lint-avoid-python-3-compatibility-imports
#
# funcslower  Trace slow kernel or user function calls.
#             For Linux, uses BCC, eBPF.
#
# USAGE: funcslower [-h] [-p PID] [-m MIN_MS] [-u MIN_US] [-a ARGUMENTS]
#                   [-T] [-t] [-v] function [function ...]
#
# WARNING: This tool traces function calls by instrumenting the entry and
# return from each function. For commonly-invoked functions like memory allocs
# or file writes, this can be extremely expensive. Mind the overhead.
#
# NOTE: This tool cannot trace nested functions in the same invocation
# due to instrumentation specifics, only innermost calls will be visible.
#
# By default, a minimum millisecond threshold of 1 is used.
#
# Copyright 2017, Sasha Goldshtein
# Licensed under the Apache License, Version 2.0 (the "License")
#
# 30-Mar-2017   Sasha Goldshtein    Created this.

from __future__ import print_function
from bcc import BPF
import argparse
import ctypes as ct
import errno
import time

examples = """examples:
  ./funcslower vfs_write        # trace vfs_write calls slower than 1ms
  ./funcslower -m 10 vfs_write  # same, but slower than 10ms
  ./funcslower -u 10 c:open     # trace open calls slower than 10us
  ./funcslower -p 135 c:open    # trace pid 135 only
  ./funcslower c:malloc c:free  # trace both malloc and free slower than 1ms
  ./funcslower -a 2 c:open      # show first two arguments to open
  ./funcslower -UK -m 10 c:open # Show user and kernel stack frame of open calls slower than 10ms
  ./funcslower -f -UK c:open    # Output in folded format for flame graphs
"""
parser = argparse.ArgumentParser(
    description="Trace slow kernel or user function calls.",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-p", "--pid", type=int, metavar="PID", dest="tgid",
    help="trace this PID only")
parser.add_argument("-m", "--min-ms", type=float, dest="min_ms",
    help="minimum duration to trace (ms)")
parser.add_argument("-u", "--min-us", type=float, dest="min_us",
    help="minimum duration to trace (us)")
parser.add_argument("-a", "--arguments", type=int,
    help="print this many entry arguments, as hex")
parser.add_argument("-T", "--time", action="store_true",
    help="show HH:MM:SS timestamp")
parser.add_argument("-t", "--timestamp", action="store_true",
    help="show timestamp in seconds at us resolution")
parser.add_argument("-v", "--verbose", action="store_true",
    help="print the BPF program for debugging purposes")
parser.add_argument(metavar="function", nargs="+", dest="functions",
    help="function(s) to trace")
parser.add_argument("--ebpf", action="store_true",
    help=argparse.SUPPRESS)
parser.add_argument("-f", "--folded", action="store_true",
    help="output folded format, one line per stack (for flame graphs)")
parser.add_argument("-U", "--user-stack",
  action="store_true", help="output user stack trace")
parser.add_argument("-K", "--kernel-stack",
  action="store_true", help="output kernel stack trace")

args = parser.parse_args()
# fractions are allowed, but rounded to an integer nanosecond
if args.min_ms:
    duration_ns = int(args.min_ms * 1000000)
elif args.min_us:
    duration_ns = int(args.min_us * 1000)
else:
    duration_ns = 1000000   # default to 1ms

bpf_text = """
#include <uapi/linux/ptrace.h>
#include <linux/sched.h>    // for TASK_COMM_LEN

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
    char name[TASK_COMM_LEN];
#ifdef USER_STACKS
    int user_stack_id;
#endif
#ifdef KERNEL_STACKS
    int kernel_stack_id;
    u64 kernel_ip;
#endif
#ifdef GRAB_ARGS
    u64 args[6];
#endif
};

BPF_HASH(entryinfo, u64, struct entry_t);
BPF_PERF_OUTPUT(events);

#if defined(USER_STACKS) || defined(KERNEL_STACKS)
BPF_STACK_TRACE(stack_traces, 2048);
#endif

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

    if (delta_ns < DURATION_NS)
        return 0;

    struct data_t data = {};
    data.id = entryp->id;
    data.tgid_pid = tgid_pid;
    data.start_ns = entryp->start_ns;
    data.duration_ns = delta_ns;
    data.retval = PT_REGS_RC(ctx);

#ifdef USER_STACKS
    data.user_stack_id = USER_STACK_GET;
#endif

#ifdef KERNEL_STACKS
    data.kernel_stack_id = KERNEL_STACK_GET;

    if (data.kernel_stack_id >= 0) {
        u64 ip = PT_REGS_IP(ctx);
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
            data.kernel_ip = ip;
        }
    }
#endif

#ifdef GRAB_ARGS
    bpf_probe_read(&data.args[0], sizeof(data.args), entryp->args);
#endif
    bpf_get_current_comm(&data.name, sizeof(data.name));
    events.perf_submit(ctx, &data, sizeof(data));

    return 0;
}
"""

bpf_text = bpf_text.replace('DURATION_NS', str(duration_ns))
if args.arguments:
    bpf_text = "#define GRAB_ARGS\n" + bpf_text
if args.user_stack:
    bpf_text = "#define USER_STACKS\n" + bpf_text
if args.kernel_stack:
    bpf_text = "#define KERNEL_STACKS\n" + bpf_text
if args.tgid:
    bpf_text = bpf_text.replace('TGID_FILTER', 'tgid != %d' % args.tgid)
else:
    bpf_text = bpf_text.replace('TGID_FILTER', '0')

# handle stack args
kernel_stack_get = "stack_traces.get_stackid(ctx, 0)"
user_stack_get = "stack_traces.get_stackid(ctx, BPF_F_USER_STACK)"
stack_context = ""
if args.user_stack:
    stack_context = "user"
    kernel_stack_get = "-1"
elif args.kernel_stack:
    stack_context = "kernel"
    user_stack_get = "-1"
else:
    stack_context = "user + kernel"
bpf_text = bpf_text.replace('USER_STACK_GET', user_stack_get)
bpf_text = bpf_text.replace('KERNEL_STACK_GET', kernel_stack_get)

for i in range(len(args.functions)):
    bpf_text += """
int trace_%d(struct pt_regs *ctx) {
    return trace_entry(ctx, %d);
}
""" % (i, i)

if args.verbose or args.ebpf:
    print(bpf_text)
    if args.ebpf:
        exit()

b = BPF(text=bpf_text)

for i, function in enumerate(args.functions):
    if ":" in function:
        library, func = function.split(":")
        b.attach_uprobe(name=library, sym=func, fn_name="trace_%d" % i)
        b.attach_uretprobe(name=library, sym=func, fn_name="trace_return")
    else:
        b.attach_kprobe(event=function, fn_name="trace_%d" % i)
        b.attach_kretprobe(event=function, fn_name="trace_return")

TASK_COMM_LEN = 16  # linux/sched.h

class Data(ct.Structure):
    _fields_ = [
        ("id", ct.c_ulonglong),
        ("pid", ct.c_ulonglong),
        ("start_ns", ct.c_ulonglong),
        ("duration_ns", ct.c_ulonglong),
        ("retval", ct.c_ulonglong),
        ("name", ct.c_char * TASK_COMM_LEN)
    ] + ([("args", ct.c_ulonglong * 6)] if args.arguments else []) + \
            ([("user_stack_id", ct.c_int)] if args.user_stack else []) + \
            ([("kernel_stack_id", ct.c_int),("kernel_ip", ct.c_ulonglong)] if args.kernel_stack else [])

time_designator = "us" if args.min_us else "ms"
time_value = args.min_us or args.min_ms or 1
time_multiplier = 1000 if args.min_us else 1000000
time_col = args.time or args.timestamp

# Do not print header when folded
if not args.folded:
    print("Tracing function calls slower than %g %s... Ctrl+C to quit." %
          (time_value, time_designator))
    print((("%-10s " % "TIME" if time_col else "") + "%-14s %-6s %7s %16s %s") %
        ("NAME", "PID", "LAT(%s)" % time_designator, "RVAL",
        "FUNC" + (" ARGS" if args.arguments else "")))

earliest_ts = 0

def time_str(event):
    if args.time:
        return "%-10s " % time.strftime("%H:%M:%S")
    if args.timestamp:
        global earliest_ts
        if earliest_ts == 0:
            earliest_ts = event.start_ns
        return "%-10.6f " % ((event.start_ns - earliest_ts) / 1000000000.0)
    return ""

def args_str(event):
    if not args.arguments:
        return ""
    return str.join(" ", ["0x%x" % arg for arg in event.args[:args.arguments]])

def stack_id_err(stack_id):
    # -EFAULT in get_stackid normally means the stack-trace is not availible,
    # Such as getting kernel stack trace in userspace code
    return (stack_id < 0) and (stack_id != -errno.EFAULT)

def aksym(addr):
    if args.annotations:
        return b.ksym(addr) + "_[k]"
    else:
        return b.ksym(addr)

need_delimiter = (args.kernel_stack and args.user_stack)
def print_stack(event):
    user_stack = []
    stack_traces = b.get_table("stack_traces")

    user_stack = [] if not args.user_stack or event.user_stack_id < 0 else \
        stack_traces.walk(event.user_stack_id)
    kernel_tmp = [] if not args.kernel_stack or event.kernel_stack_id < 0 else \
        stack_traces.walk(event.kernel_stack_id)

    # fix kernel stack
    kernel_stack = []
    if args.kernel_stack and event.kernel_stack_id >= 0:
        for addr in kernel_tmp:
            kernel_stack.append(addr)

    if args.folded:
        # print folded stack output
        user_stack = list(user_stack)
        kernel_stack = list(kernel_stack)
        line = [event.name.decode()]
        # if we failed to get the stack is, such as due to no space (-ENOMEM) or
        # hash collision (-EEXIST), we still print a placeholder for consistency
        if args.user_stack:
            if stack_id_err(event.user_stack_id):
                line.append("[Missed User Stack]")
            else:
                line.extend([str(b.sym(addr, event.pid)) for addr in reversed(user_stack)])
        if args.kernel_stack:
            line.extend(["-"] if (need_delimiter and event.kernel_stack_id >= 0 and
                    event.user_stack_id >= 0) else [])
            if stack_id_err(event.kernel_stack_id):
                line.append("[Missed Kernel Stack]")
            else:
                line.extend([str(b.ksym(addr)) for addr in reversed(kernel_stack)])
        print("%s %d" % (str(";".join(line)), 1))
    else:
        # print default multi-line stack output
        if not args.user_stack:
            if stack_id_err(event.kernel_stack_id):
                print("    [Missed Kernel Stack]")
            else:
                for addr in kernel_stack:
                    print("    %s" % aksym(addr))
        if not args.kernel_stack:
            if need_delimiter and event.user_stack_id >= 0 and event.kernel_stack_id >= 0:
                print("    --")
            if stack_id_err(event.user_stack_id):
                print("    [Missed User Stack]")
            else:
                for addr in user_stack:
                    print("    %s" % b.sym(addr, event.pid))
        print("    %-16s %s (%d)" % ("-", event.name.decode(), event.pid))

def print_event(cpu, data, size):
    event = ct.cast(data, ct.POINTER(Data)).contents
    ts = float(event.duration_ns) / time_multiplier
    if not args.folded:
        print((time_str(event) + "%-14.14s %-6s %7.2f %16x %s %s") %
            (event.name.decode(), event.pid >> 32,
             ts, event.retval, args.functions[event.id], args_str(event)))
    if args.user_stack or args.kernel_stack:
        print_stack(event)

b["events"].open_perf_buffer(print_event, page_cnt=64)
while True:
    b.perf_buffer_poll()
