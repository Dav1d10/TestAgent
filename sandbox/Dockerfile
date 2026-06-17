FROM python:3.11-slim

WORKDIR /sandbox

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source code and tests are mounted at runtime, not baked into the image.
# This avoids rebuilding the image every time the agent generates something new.

# Run as a non-root user for basic security isolation.
RUN useradd -m sandboxuser
USER sandboxuser

CMD ["pytest", "--tb=short", "-v", "-p", "no:cacheprovider"]
