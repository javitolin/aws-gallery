SHELL := /bin/bash

# Personal settings live in config.env, which is gitignored.
ifneq (,$(wildcard config.env))
include config.env
export
endif

VENV := .venv/bin

.PHONY: help venv layers test dev plan apply site backfill publish publish-check transcode cert-status outputs

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

venv: ## create the local python env used by the scripts
	python3 -m venv .venv && $(VENV)/pip install -q --upgrade pip boto3

layers: ## build the three lambda layers (no container runtime needed)
	./layers/build.sh

test: ## run the lambda and frontend unit tests
	$(VENV)/python -m unittest discover -s tests
	node tests/nesting.test.mjs

plan: ## show pending infrastructure changes
	cd terraform && terraform plan

apply: ## apply infrastructure
	cd terraform && terraform apply

site: ## publish site/ and invalidate the edge
	./scripts/deploy-site.sh

backfill: ## regenerate thumbnails and metadata for existing media
	$(VENV)/python scripts/backfill.py

dev: ## run the gallery locally against real S3 content
	$(VENV)/python scripts/dev_server.py

publish-check: ## dry run: what would be transcoded and uploaded
	$(VENV)/python scripts/publish.py

publish: ## transcode what needs it, then upload. safe to re-run
	$(VENV)/python scripts/publish.py --apply

transcode: ## only prepare the web-ready tree, no upload
	$(VENV)/python scripts/transcode.py --apply

cert-status: ## acm validation state
	@aws acm list-certificates --query "CertificateSummaryList[?DomainName=='$(GALLERY_DOMAIN)'].CertificateArn" --output text \
	  | xargs -I{} aws acm describe-certificate --certificate-arn {} --query 'Certificate.Status' --output text

outputs: ## terraform outputs
	cd terraform && terraform output
