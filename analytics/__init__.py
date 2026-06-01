"""Phoenix analytics package.

Home for read-only analytical libraries that the Phoenix Strategy Oracle
(and other higher-level tools) call when they need quantitative views over
the DuckDB warehouse. Nothing in this package writes to the warehouse.

Architectural invariant: this package must NOT import from
`bots/`, `core/`, `bridge/`, or `data_feeds/`. It depends only on
`tools.warehouse` (schema-related helpers) and standard data libraries.
"""
