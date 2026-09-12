.PHONY: up up-m1 up-m1-m2 up-m1-m2-m3 down build logs status demo demo-install

up:
	docker compose -f docker-compose.yml up -d

up-m1:
	docker compose -f docker-compose.m1.yml up -d

up-m1-m2:
	docker compose -f docker-compose.m1-m2.yml up -d

up-m1-m2-m3:
	docker compose -f docker-compose.m1-m2-m3.yml up -d

down:
	docker compose -f docker-compose.yml down

build:
	docker compose -f docker-compose.yml build

logs:
	docker compose -f docker-compose.yml logs -f

status:
	docker compose -f docker-compose.yml ps

demo-install:
	cd opt-demo && pip install -r requirements.txt

demo:
	cd opt-demo && streamlit run chat_ui.py
