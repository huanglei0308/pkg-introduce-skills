FROM python:3.10-slim

WORKDIR /app

# Python 依赖
COPY docker/importer-worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Worker 代码
COPY docker/importer-worker/worker.py docker/importer-worker/job_runner.py docker/importer-worker/copr_client.py ./

# Skills & Agents（替代 hostPath 挂载）
COPY .claude/skills/ .claude/skills/
COPY .claude/agents/ .claude/agents/

CMD ["python3", "worker.py"]
