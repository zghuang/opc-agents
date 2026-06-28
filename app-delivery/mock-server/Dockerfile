FROM python:3.14-slim
WORKDIR /app
RUN pip install --no-cache-dir \
	"fastapi>=0.136.3,<1.0" \
	"uvicorn[standard]>=0.48.0,<1.0" \
	"pydantic>=2.13.4,<3.0" \
	"httpx>=0.28.1,<1.0" \
	"pytest>=9.0.3,<10.0" \
	"pytest-asyncio>=1.4.0,<2.0" \
	"pytest-httpx>=0.32" \
	"requests>=2.31" \
	"faker>=26.0"
COPY . .
EXPOSE 8888
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8888"]
