.PHONY: test compile doctor doctor-full deploy

test:
	PYTHONPATH=. pytest -q

compile:
	python -m compileall -q cloky tests
	bash -n scripts/*.sh

doctor:
	python -m cloky.doctor

doctor-full:
	python -m cloky.doctor --full

deploy:
	bash scripts/deploy.sh "$(HOME)/cloky-elite-telegram-bot"
