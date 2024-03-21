import os
import json
import logging
import sys
from typing import List
import urllib

import boto3

from langchain_community.vectorstores import PGVector
from langchain_community.embeddings import SagemakerEndpointEmbeddings
from langchain_community.embeddings.sagemaker_endpoint import EmbeddingsContentHandler

from langchain.llms.sagemaker_endpoint import (
    SagemakerEndpoint,
    LLMContentHandler
)

from langchain.prompts import PromptTemplate
from langchain.chains import ConversationalRetrievalChain

logger = logging.getLogger()
logging.basicConfig(format='%(asctime)s,%(module)s,%(processName)s,%(levelname)s,%(message)s', level=logging.INFO, stream=sys.stderr)


class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


MAX_HISTORY_LENGTH = 5


def _create_sagemaker_embeddings(endpoint_name: str, region: str = "us-east-1") -> SagemakerEndpointEmbeddings:

    class ContentHandlerForEmbeddings(EmbeddingsContentHandler):
        """
        encode input string as utf-8 bytes, read the embeddings
        from the output
        """

        content_type = "application/json"
        accepts = "application/json"

        def transform_input(self, prompt: str, model_kwargs={}) -> bytes:
            input_str = json.dumps({"text_inputs": prompt, **model_kwargs})
            return input_str.encode('utf-8')

        def transform_output(self, output: bytes) -> str:
            response_json = json.loads(output.read().decode("utf-8"))
            embeddings = response_json["embedding"]
            if len(embeddings) == 1:
                return [embeddings[0]]
            return embeddings

    # create a content handler object which knows how to serialize
    # and deserialize communication with the model endpoint
    content_handler = ContentHandlerForEmbeddings()

    # read to create the Sagemaker embeddings, we are providing
    # the Sagemaker endpoint that will be used for generating the
    # embeddings to the class
    #
    embeddings = SagemakerEndpointEmbeddings(
        endpoint_name=endpoint_name,
        region_name=region,
        content_handler=content_handler
    )
    logger.info(f"embeddings type={type(embeddings)}")

    return embeddings


def _get_credentials(secret_id: str, region_name: str = 'us-east-1') -> str:
    client = boto3.client('secretsmanager', region_name=region_name)
    response = client.get_secret_value(SecretId=secret_id)
    secrets_value = json.loads(response['SecretString'])
    return secrets_value


def build_chain():
    region = os.environ["AWS_REGION"]
    embeddings_model_endpoint = os.environ["EMBEDDING_ENDPOINT_NAME"]
    text2text_model_endpoint = os.environ["TEXT2TEXT_ENDPOINT_NAME"]

    pgvector_secret_id = os.environ["PGVECTOR_SECRET_ID"]
    secret = _get_credentials(pgvector_secret_id, region)
    db_username = secret['username']
    db_password = urllib.parse.quote_plus(secret['password'])
    db_port = secret['port']
    db_host = secret['host']

    CONNECTION_STRING = PGVector.connection_string_from_db_params(
        driver = 'psycopg2',
        user = db_username,
        password = db_password,
        host = db_host,
        port = db_port,
        database = ''
    )

    collection_name = os.environ["COLLECTION_NAME"]

    class ContentHandler(LLMContentHandler):
        content_type = "application/json"
        accepts = "application/json"

        def transform_input(self, prompt: str, model_kwargs: dict) -> bytes:
            input_str = json.dumps({"text_inputs": prompt, **model_kwargs})
            return input_str.encode('utf-8')

        def transform_output(self, output: bytes) -> str:
            response_json = json.loads(output.read().decode("utf-8"))
            return response_json["generated_texts"][0]

    content_handler = ContentHandler()

    model_kwargs = {
      "max_length": 500,
      "num_return_sequences": 1,
      "top_k": 250,
      "top_p": 0.95,
      "do_sample": False,
      "temperature": 1
    }

    llm = SagemakerEndpoint(
        endpoint_name=text2text_model_endpoint,
        region_name=region,
        model_kwargs=model_kwargs,
        content_handler=content_handler
    )

    vectorstore = PGVector(
      collection_name=collection_name,
      connection_string=CONNECTION_STRING,
      embedding_function=_create_sagemaker_embeddings(embeddings_model_endpoint, region)
    )
    retriever = vectorstore.as_retriever()

    prompt_template = """Answer based on context:\n\n{context}\n\n{question}"""

    PROMPT = PromptTemplate(
        template=prompt_template, input_variables=["context", "question"]
    )

    condense_qa_template = """
    Given the following conversation and a follow up question, rephrase the follow up question
    to be a standalone question.

    Chat History:
    {chat_history}
    Follow Up Input: {question}
    Standalone question:"""
    standalone_question_prompt = PromptTemplate.from_template(condense_qa_template)

    qa = ConversationalRetrievalChain.from_llm(
        llm=llm,
        retriever=retriever,
        condense_question_prompt=standalone_question_prompt,
        return_source_documents=True,
        combine_docs_chain_kwargs={"prompt":PROMPT}
    )

    logger.info(f"\ntype('qa'): \"{type(qa)}\"\n")
    return qa


def run_chain(chain, prompt: str, history=[]):
   return chain({"question": prompt, "chat_history": history})


if __name__ == "__main__":
    chat_history = []
    qa = build_chain()
    print(bcolors.OKBLUE + "Hello! How can I help you?" + bcolors.ENDC)
    print(bcolors.OKCYAN + "Ask a question, start a New search: or CTRL-D to exit." + bcolors.ENDC)
    print(">", end=" ", flush=True)
    for query in sys.stdin:
        if (query.strip().lower().startswith("new search:")):
            query = query.strip().lower().replace("new search:","")
            chat_history = []
        elif (len(chat_history) == MAX_HISTORY_LENGTH):
            chat_history.pop(0)
        result = run_chain(qa, query, chat_history)
        chat_history.append((query, result["answer"]))
        print(bcolors.OKGREEN + result['answer'] + bcolors.ENDC)
        if 'source_documents' in result:
            print(bcolors.OKGREEN + 'Sources:')
            for d in result['source_documents']:
                print(d.metadata['source'])
        print(bcolors.ENDC)
        print(bcolors.OKCYAN + "Ask a question, start a New search: or CTRL-D to exit." + bcolors.ENDC)
        print(">", end=" ", flush=True)
    print(bcolors.OKBLUE + "Bye" + bcolors.ENDC)