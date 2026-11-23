import yaml

config = yaml.safe_load("status: ready")
print(f"应用启动成功，状态为 {config['status']}")
