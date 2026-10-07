#!/usr/bin/env python3
"""The command handlers, one module per domain.

Each handler keeps the shape `command_x(args, root) -> int` that
`build_parser` binds through `set_defaults(handler=...)`, so the CLI stays a
thin dispatcher and the modules below it can be called directly.
"""
