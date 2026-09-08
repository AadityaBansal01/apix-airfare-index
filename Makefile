.PHONY: help init db-up db-down db-reset schema reference compliance weights pipeline-demo scrape scrape-plan backfill cron base index series api api-check dashboard test backtest nowcast load-cpi load-dgca demo clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

init:  ## install python deps + playwright browsers
	python -m pip install -r requirements.txt
	python -m playwright install chromium

db-up:  ## start postgres
	@# --wait blocks until the container's healthcheck passes, which is the only
	@# reliable readiness signal here.
	@#
	@# On a fresh volume Postgres logs "database system is ready to accept
	@# connections" TWICE: once for a temporary server it runs during
	@# initialisation, then again after shutting that down and starting the real
	@# one. A hand-rolled pg_isready loop answers on the first, so schema and
	@# reference data written in that window are silently discarded when the
	@# real server takes over. That produced a "relation apix.fare does not
	@# exist" failure one step after the table had been populated successfully.
	docker compose up -d --wait postgres
	@echo "postgres ready on :5433"

db-down:  ## stop postgres
	docker compose down

db-reset:  ## destroy and recreate the database
	docker compose down -v && $(MAKE) db-up

schema: db-up  ## (re)load the schema
	docker compose exec -T postgres psql -U apix -d apix -v ON_ERROR_STOP=1 < db/schema.sql
	@echo "schema loaded"

reference: schema  ## load airports, routes, weights and the source registry
	python scripts/load_reference.py

compliance:  ## print the ethical-scraping evidence (the judge-facing query)
	@docker compose exec -T postgres psql -U apix -d apix -c "SELECT * FROM apix.v_compliance_summary ORDER BY source;"

weights:  ## recompute route weights from DGCA traffic
	python scripts/build_route_weights.py

pipeline-demo:  ## show the cleaning report on a synthetic day of quotes
	python scripts/demo_cleaning.py

rebuild:  ## reset db, seed 66 days, build base prices and index
	./scripts/rebuild_demo.sh

evidence:  ## make real robots.txt/audit evidence against live sources
	python scripts/compliance_evidence.py

load-cpi:  ## load the official CPI Transport series for the overlay (needs a CSV)
	@echo "Download Division 07 Transport, monthly, from:"
	@echo "  https://esankhyiki.mospi.gov.in/macroindicators?product=cpi"
	@echo "then: python scripts/load_cpi.py --csv <file>"

export:  ## dump the built index to dashboard/public/data as static JSON
	python scripts/export_static.py

deploy: export  ## export, build and deploy the dashboard to Vercel
	cd dashboard && npm run build && VERCEL_TELEMETRY_DISABLED=1 vercel deploy --prod --yes

scrape:  ## run one daily collection cycle over the route x window matrix
	python -m apix.cli scrape --config config/basket.yaml

scrape-plan:  ## print what a cycle would collect and how long it would take (no network)
	python -m apix.cli scrape --config config/basket.yaml --dry-run

backfill:  ## assess yesterday's collection; re-run if recoverable, else record the gap
	python -m apix.cli backfill --date yesterday --config config/basket.yaml

cron:  ## print the crontab that runs collection daily at 02:00 IST
	python -m apix.cli schedule --print-cron

base:  ## compute base prices over the price reference period
	python -m apix.cli base --config config/basket.yaml

index:  ## build elementary, headline, weekly and monthly indices
	python -m apix.cli index --config config/basket.yaml

series:  ## print the built daily index series
	python -m apix.cli series --frequency daily

api:  ## serve the public API on :8000 (docs at /docs)
	uvicorn apix.api.main:app --reload --port 8000

api-check:  ## smoke-test every API endpoint against a running server
	./scripts/api_smoke.sh

dashboard:  ## serve the React dashboard on :5173
	cd dashboard && npm install && npm run dev

test:  ## run the test suite
	python -m pytest tests/ -v

nowcast:  ## score short-horizon forecasts against the naive baselines
	python -m apix.cli nowcast

backtest:  ## run the back-test and validation suite (chart + report)
	python scripts/backtest.py --out data/backtest

load-dgca:  ## load reference fares for the external comparison (needs a CSV)
	@echo "No public DGCA route-wise monthly average-fare series was found."
	@echo "If you obtain one (parliamentary answer, TMU extract, licensed feed):"
	@echo "  python scripts/load_dgca_fares.py --csv <file> \\"
	@echo "    --fare-basis '<what average fare means here>' --source-url '<url>'"

demo: db-up schema reference  ## seeded end-to-end demo, no network required (~10s)
	python -m apix.cli seed --start 2026-07-01 --end 2026-09-04 \
		--surge-start 2026-08-20 --surge-days 8 --surge-peak 1.45
	python -m apix.cli base
	python -m apix.cli index
	@echo ""
	@$(MAKE) --no-print-directory series
	@echo ""
	@echo "next: 'make api' then open http://localhost:8000/docs"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .coverage
