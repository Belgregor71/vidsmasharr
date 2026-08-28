"""Phase 2: decide what to do with each file, and in what order.

Nothing in this package touches the media library. It reads probe facts,
applies policy, and writes rows to the `decision` table -- a plan you can read
before anything acts on it. Phase 3 is what will execute it.
"""
