import os
import asyncio
from typing import Type
from datetime import datetime
from dataclasses import dataclass, field, asdict
from .prompt import prompts
from ._llm import gpt_4o_complete, gpt_4o_mini_complete
from ._utils import (
    limit_async_func_call,
    generate_id,
    EmbeddingFunc,
    logger,
)
from .storage import JsonKVStorage, BaseKVStorage, BaseVectorStorage, MilvusLiteStorge
from ._ops import chunking_by_token_size, openai_embedding


@dataclass
class GraphRAG:
    working_dir: str = field(
        default_factory=lambda: f"./nano_graphrag_cache_{datetime.now().strftime('%Y-%m-%d-%H:%M:%S')}"
    )

    chunk_token_size: int = 1200
    chunk_overlap_token_size: int = 100
    tiktoken_model_name: str = "gpt-4o"

    embedding_func: EmbeddingFunc = openai_embedding
    embedding_batch_num: int = 16
    embedding_func_max_async: int = 8

    best_model_func: callable = gpt_4o_complete
    best_model_max_async: int = 8
    cheap_model_func: callable = gpt_4o_mini_complete
    cheap_model_max_async: int = 8

    key_string_value_json_storage_cls: Type[BaseKVStorage] = JsonKVStorage
    vector_db_storage_cls: Type[BaseVectorStorage] = MilvusLiteStorge

    def __post_init__(self):
        self.embedding_func = limit_async_func_call(self.embedding_func_max_async)(
            self.embedding_func
        )
        self.best_model_func = limit_async_func_call(
            max_size=self.best_model_max_async
        )(self.best_model_func)
        self.cheap_model_func = limit_async_func_call(
            max_size=self.cheap_model_max_async
        )(self.cheap_model_func)

        if not os.path.exists(self.working_dir):
            logger.info(f"Creating working directory {self.working_dir}")
            os.makedirs(self.working_dir)

        self.full_docs = self.key_string_value_json_storage_cls(
            namespace="full_docs", global_config=asdict(self)
        )
        self.text_chunks = self.key_string_value_json_storage_cls(
            namespace="text_chunks", global_config=asdict(self)
        )
        self.text_chunks_vdb = self.vector_db_storage_cls(
            namespace="text_chunks",
            global_config=asdict(self),
            embedding_func=self.embedding_func,
        )
        logger.info(f"GraphRAG init done with param: {asdict(self)}")

    async def aquery(self, query: str):
        return await self.best_model_func(query)

    def query(self, query: str):
        return asyncio.run(self.aquery(query))

    async def ainsert(self, string_or_strings):
        if isinstance(string_or_strings, str):
            string_or_strings = [string_or_strings]
        new_docs = {
            generate_id(prefix="doc-"): {"content": c.strip()}
            for c in string_or_strings
        }

        inserting_chunks = {}
        for doc_key, doc in new_docs.items():
            chunks = {
                generate_id(prefix="chunk-"): {**dp, "full_doc_id": doc_key}
                for dp in chunking_by_token_size(
                    doc["content"],
                    overlap_token_size=self.chunk_overlap_token_size,
                    max_token_size=self.chunk_token_size,
                    tiktoken_model=self.tiktoken_model_name,
                )
            }
            inserting_chunks.update(chunks)
        # upsert to vector db
        await self.text_chunks_vdb.upsert(inserting_chunks)
        # upsert to KV
        await self.full_docs.upsert(new_docs)
        await self.text_chunks.upsert(chunks)
        logger.info(
            f"Process {len(new_docs)} new docs, add {len(inserting_chunks)} new chunks"
        )

    def insert(self, string_or_strings):
        return asyncio.run(self.ainsert(string_or_strings))