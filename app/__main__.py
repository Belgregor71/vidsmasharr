"""So `python -m app <command>` works, which is what the container's
ENTRYPOINT (`python3 -m`) turns `docker compose run vidsmasharr app scan` into."""

import sys

from app.cli import main

if __name__ == "__main__":
    sys.exit(main())
