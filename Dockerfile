FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 zhiyuan \
    && install -d -o zhiyuan -g zhiyuan /app/data
COPY app.py chat_history.py diagnosis.py image_diagnosis.py model_observer.py web_server.py knowledge_admin.py knowledge_answer.py knowledge_base.py usage_guidance.py ./
COPY static ./static
USER 10001
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "web_server:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1", "--no-access-log"]
