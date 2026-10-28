import sys
import traceback
import os 

def getExceptionMessage(types):
    # Get current system exception
    ex_type, ex_value, ex_traceback = sys.exc_info()

    # Extract unformatter stack traces as tuples
    trace_back = traceback.extract_tb(ex_traceback)

    # Format stacktrace
    stack_trace = list()

    for trace in trace_back:
        stack_trace.append("File : %s , Line : %d, Func.Name : %s, Message : %s" % (trace[0], trace[1], trace[2], trace[3]))

    if "type" in types: print("Exception type : %s " % ex_type.__name__)
    if "message" in types: print("Exception message : %s" %ex_value)
    if "stacktrace" in types: print("Stack trace : %s" %stack_trace)

def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"Missing environment variable {name}")
    return v
