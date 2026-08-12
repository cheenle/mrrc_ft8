"""PyInstaller entry for the MRRC_FT8 server.

PyInstaller executes the spec's script as a top-level module, so a script
inside the ``server`` package cannot use relative imports (server/main.py has
17 ``from .engine...`` imports that are valid only under ``python -m
server.main``).  This thin wrapper imports ``server.main`` as a package and
forwards to its CLI; ``main()`` reads sys.argv itself (argparse), so the
launcher's ``--ssl-cert``/``--ssl-key`` args are passed through unchanged.

``multiprocessing.freeze_support()`` must run here: it is the frozen
``__main__`` (server/main.py's own ``__main__`` guard never fires when it is
imported as a package), and it strips the ``--multiprocessing-fork`` args the
PyInstaller bootloader passes to spawned DSP/capture child processes.
"""

if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()
    from server.main import main

    raise SystemExit(main())
