lint:
	poetry run isort -rc --atomic .
	poetry run black .

build:
	poetry build

publish:
	poetry publish