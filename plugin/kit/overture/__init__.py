"""The Overture owner-operator kit: per-item guidance threads with locked answers (spec `architect/40-specs/owner-console.md`).

This is a package rather than loose modules, so that its `schema` can never
shadow a host project's own module of the same name. A project plugs in
through one adapter module (`fold.ProjectAdapter`).
"""

__version__ = "1.25.0"  # kept equal to VERSION and plugin.json by test_build; /health reports it
