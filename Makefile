PY?=python3
PIP?=$(PY) -m pip

.PHONY: install run test clean logs

install:
	$(PIP) install -r requirements.txt

run:
	$(PY) app_ultimate_enhanced.py

test:
	$(PY) -m unittest -v

clean:
	rm -rf __pycache__ */__pycache__ .pytest_cache build dist *.egg-info

logs:
	mkdir -p logs && ls -la logs

