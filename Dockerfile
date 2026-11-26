FROM python:3.12-slim

WORKDIR /app
COPY agent.py .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "-u", "agent.py"]
