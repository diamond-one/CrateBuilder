import os
import sys

# Windowed executables have no console streams. Some GUI/audio dependencies
# write harmless warnings while importing; provide a valid stream for them.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

from cratebuilder.__main__ import main

if __name__ == "__main__":
    main()
