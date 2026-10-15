.PHONY: test images k3s-import deploy workloads monitoring undeploy scenario
test:
	python -m pytest -q
images:
	docker build -t lifeboat/controller:latest .
	docker build -t lifeboat/webapp:latest workloads/webapp
	docker build -t lifeboat/worker:latest -f workloads/worker/Dockerfile .
k3s-import: images
	for i in controller webapp worker; do docker save lifeboat/$$i:latest | sudo k3s ctr images import -; done
deploy:
	kubectl apply -f deploy/00-namespaces-crd.yaml -f deploy/01-priorityclasses.yaml -f deploy/02-rbac.yaml -f deploy/03-lifeboat.yaml
workloads:
	kubectl apply -f deploy/04-demo-workloads.yaml
monitoring:
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm upgrade --install kps prometheus-community/kube-prometheus-stack -n monitoring --create-namespace
	kubectl apply -f deploy/monitoring/podmonitors.yaml
scenario:
	python scripts/make_scenario.py
undeploy:
	kubectl delete -f deploy/04-demo-workloads.yaml -f deploy/03-lifeboat.yaml --ignore-not-found
