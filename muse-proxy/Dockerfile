FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir tzdata
COPY proxy.py allowed.json ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8765
USER nobody
CMD ["python", "-I", "proxy.py"]
