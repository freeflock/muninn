FROM python:3.11-slim
WORKDIR /atelier

COPY ./requirements.txt /atelier/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /atelier/requirements.txt

COPY ./packages/muninn /atelier/muninn

CMD ["python3", "-m", "muninn.main"]
