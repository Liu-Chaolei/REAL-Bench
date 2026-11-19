"""Compatibility entry point for the provider-neutral Gaia2 async report."""

from report_gaia2_async import aggregate, main  # noqa: F401

if __name__ == "__main__":
    main()
