import os
import traceback
from time import sleep

import requests
from langchain import hub
from langchain_openai import ChatOpenAI
from loguru import logger
from ratatosk_errands.adapter import Rabbit
from ratatosk_errands.model import Errand, Echo, DiscoveryInstructions, DiscoveryReply
from tavily import TavilyClient

RABBIT_HOST = os.getenv("RABBIT_HOST")
RABBIT_PORT = int(os.getenv("RABBIT_PORT"))
RABBIT_USERNAME = os.getenv("RABBIT_USERNAME")
RABBIT_PASSWORD = os.getenv("RABBIT_PASSWORD")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
USE_SCRAPER_API_KEY = os.getenv("USE_SCRAPER_API_KEY")


def generate_supporting_search_queries(message):
    prompt = hub.pull("supporting_discovery")
    model = ChatOpenAI(model="gpt-4o")
    chain = prompt | model
    output = chain.invoke({"message": message})
    return output.get("search_queries")


def generate_opposing_search_queries(message):
    prompt = hub.pull("opposing_discovery")
    model = ChatOpenAI(model="gpt-4o")
    chain = prompt | model
    output = chain.invoke({"message": message})
    return output.get("search_queries")


def synopsize_search(search_query, web_content):
    prompt = hub.pull("search_synopsis")
    model = ChatOpenAI(model="gpt-4o")
    chain = prompt | model
    output = chain.invoke({"search_query": search_query, "web_content": web_content})
    return output.get("synopsis")


def search(search_query):
    try:
        tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
        search_results_wrapper = tavily_client.search(search_query)
        search_result = search_results_wrapper.get("results", [{}])
        urls = [result.get("url") for result in search_result]
        urls = urls[:3]
        logger.info(f"tavily returned these urls: {urls}")
        scrape_results = []
        for url in urls:
            if url is not None:
                logger.info(f"scraping url: {url}")
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {USE_SCRAPER_API_KEY}"
                }
                payload = {
                    "format": "markdown",
                    "advanced_proxy": False,
                    "url": url
                }
                response = requests.post("https://api.usescraper.com/scraper/scrape", json=payload, headers=headers)
                if "application/json" not in response.headers.get("content-type"):
                    logger.warning(f"no json in response for {url}: {response.text}")
                else:
                    response_json = response.json()
                    if response_json["status"] != "scraped":
                        logger.warning(f"scrape failed for {url}: {response.text}")
                    else:
                        scrape_results.append(response_json["text"])
        web_content = "\n\n".join(scrape_results)
        synopsis = synopsize_search(search_query, web_content)
        logger.info(f"generated synopsis for search query: {search_query}\n\n{synopsis}")
        return synopsis
    except Exception as error:
        logger.error(f"search encountered error: {error}")
        return ""


def receive_discovery_errand(channel, method, properties, body):
    try:
        logger.info(f"( ) receiving errand: {body}")
        errand = Errand.model_validate_json(body)
        if not isinstance(errand.instructions, DiscoveryInstructions):
            raise ValueError(f"unknown errand instructions on errand: {errand}")

        logger.info(f"determining supporting search queries")
        supporting_search_queries = generate_supporting_search_queries(errand.instructions.message)
        logger.info("supporting:")
        logger.info(supporting_search_queries)

        logger.info(f"determining opposing search queries")
        opposing_search_queries = generate_opposing_search_queries(errand.instructions.message)
        logger.info("opposing:")
        logger.info(opposing_search_queries)

        logger.info(f"running search")
        supporting_search_queries = supporting_search_queries[:3]
        opposing_search_queries = opposing_search_queries[:3]
        discovery_result = []
        for search_query in supporting_search_queries + opposing_search_queries:
            synopsis = search(search_query)
            discovery_result.append(synopsis)
        logger.info(discovery_result)
        reply = DiscoveryReply(discovery_result=discovery_result)
        echo = Echo(errand=errand, reply=reply)
        channel.basic_publish(exchange="", routing_key="echo", body=echo.model_dump_json())

        logger.info(f"(*) completed errand: {errand.errand_identifier}")
    except Exception as error:
        logger.error(f"(!) errand failed with error: {error}")
    finally:
        channel.basic_ack(delivery_tag=method.delivery_tag)


def main():
    while True:
        try:
            with Rabbit(RABBIT_HOST, RABBIT_PORT, RABBIT_USERNAME, RABBIT_PASSWORD) as rabbit:
                rabbit.channel.basic_qos(prefetch_count=1)
                rabbit.channel.queue_declare(queue="echo")
                rabbit.channel.queue_declare(queue="discovery")
                rabbit.channel.basic_consume(queue="discovery",
                                             on_message_callback=receive_discovery_errand)
                logger.info(f"setup complete, listening for errands")
                rabbit.channel.start_consuming()
        except Exception as error:
            logger.error(f"(!) rabbit connection failed with error: {error}\n{traceback.format_exc()}")
            sleep(3)


if __name__ == '__main__':
    main()
