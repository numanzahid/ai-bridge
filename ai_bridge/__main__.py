import sys

if len(sys.argv) > 1:
    from .cli import main
    raise SystemExit(main())
else:
    from .server import main
    main()
