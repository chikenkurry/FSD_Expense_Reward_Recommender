# Testing

The baseline is stdlib `unittest` and has no network dependency except loopback fixture servers in scraper/API tests. Run with `-W error::ResourceWarning` to catch leaks. `scripts/verify.py` checks committed JSON artifacts and route parity. Coverage is optional development tooling: `python -m pip install 'coverage>=7.6,<8'`; then run `coverage run -m unittest discover -s tests && coverage report --include='credit_card_service/*'`. The target for the expanded suite is at least 85% lines.
