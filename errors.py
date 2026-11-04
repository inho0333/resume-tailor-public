"""Errors that are the user's setup, not a bug in the code.

A misconfigured path, a base_resume.yaml that does not match the document, no
LLM backend available — these are things the person running the tool has to fix,
and a Python traceback is the wrong way to tell them. Anything raised as a
SetupError is printed by run.py as a plain message plus a pointer to
`run.py doctor`, and the process exits 1.

Genuine bugs keep their traceback. That distinction is the point of this file:
if you cannot write a sentence telling the user what to change, it is not a
SetupError.
"""


class SetupError(RuntimeError):
    """Something about this installation is wrong. The message is the fix."""
