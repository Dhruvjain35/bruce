# Bruce — top-level entrypoints.
#
# The one that matters tomorrow morning:
#
#     make deploy-fc-dry     # every local check, never contacts Alibaba
#     make deploy-fc         # preflight -> build -> deploy -> LIVE verify -> proof file
#
# `make deploy-fc` is deliberately the ONLY step needed after Function Compute is activated. It
# either prints a live URL that has answered /health, or names exactly which wall it hit.

.PHONY: help test test-offline deploy-fc deploy-fc-dry package ios smoke qwen-smoke clean

ENGINE := engine
PY     := $(ENGINE)/.venv/bin/python

help:
	@echo "make test          — full backend suite (real Postgres if available)"
	@echo "make test-offline  — suite without live/deploy-dependent tests"
	@echo "make package       — build the Function Compute code package (linux/amd64)"
	@echo "make deploy-fc-dry — full local validation, NO Alibaba contact"
	@echo "make deploy-fc     — deploy to Function Compute + verify the live URL"
	@echo "make smoke         — smoke test a live deployment (needs BRUCE_DEPLOY_URL)"
	@echo "make qwen-smoke    — ONE bounded live Qwen call (blocked until the account is entitled)"
	@echo "make ios           — build the iOS app"

test:
	cd $(ENGINE) && .venv/bin/python -m pytest -q

test-offline:
	cd $(ENGINE) && .venv/bin/python -m pytest -q \
	  --ignore=tests/test_deployment_smoke.py

package:
	./deploy/build-package.sh

deploy-fc-dry:
	./deploy/deploy_fc.sh --dry-run

deploy-fc:
	./deploy/deploy_fc.sh

smoke:
	@test -n "$$BRUCE_DEPLOY_URL" || (echo "set BRUCE_DEPLOY_URL first (see docs/deployment-proof.json)"; exit 1)
	cd $(ENGINE) && .venv/bin/python -m pytest tests/test_deployment_smoke.py -v

# ONE bounded live call. Does not enumerate models. Prints a sanitized result only.
qwen-smoke:
	cd $(ENGINE) && .venv/bin/python -m scripts.qwen_smoke

ios:
	cd ios && xcodegen generate && xcodebuild -project Bruce.xcodeproj -scheme Bruce \
	  -destination 'generic/platform=iOS Simulator' -configuration Debug build | tail -3

clean:
	rm -rf build/fc build/bruce-fc.zip
