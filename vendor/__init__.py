"""Code this project needs but does not own.

Everything in here used to live outside the repository and was loaded by absolute
path, which meant a fresh clone could not even run `python run.py --help`. These
are self-contained copies: no imports from outside the repo, no shared state.
"""
