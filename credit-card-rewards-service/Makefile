.PHONY: test verify demo benchmark
test:
	python3 -m unittest discover -s tests -v
verify:
	python3 -m compileall -q credit_card_service tests scripts
	python3 scripts/verify.py
demo:
	python3 scripts/demo_acceptance.py
benchmark:
	python3 scripts/benchmark.py
