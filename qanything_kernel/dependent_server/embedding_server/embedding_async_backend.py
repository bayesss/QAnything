import asyncio
import time
import numpy as np
from onnxruntime import SessionOptions, GraphOptimizationLevel, InferenceSession
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from transformers import AutoTokenizer
from qanything_kernel.utils.custom_log import debug_logger
from qanything_kernel.configs.model_config import LOCAL_EMBED_MAX_LENGTH, LOCAL_EMBED_PATH, LOCAL_EMBED_BATCH
from qanything_kernel.utils.general_utils import get_time


class EmbeddingAsyncBackend:
    def __init__(self, model_path, use_cpu=True, num_threads=4):
        self.use_cpu = use_cpu
        self.return_tensors = "np"
        sess_options = SessionOptions()
        sess_options.graph_optimization_level = GraphOptimizationLevel.ORT_ENABLE_ALL

        if use_cpu:
            providers = ['CPUExecutionProvider']
            self.executor = ThreadPoolExecutor(max_workers=num_threads)
            self.batch_size = LOCAL_EMBED_BATCH  # CPU批处理大小
        else:
            providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
            self.executor = ProcessPoolExecutor(max_workers=1)
            self.batch_size = LOCAL_EMBED_BATCH  # GPU批处理大小

        self.session = InferenceSession(model_path, sess_options=sess_options, providers=providers)
        self._tokenizer = AutoTokenizer.from_pretrained(LOCAL_EMBED_PATH)  # 请根据实际使用的模型调整

        self.queue = asyncio.Queue()
        asyncio.create_task(self.process_queue())

    async def embed_documents_async(self, texts):
        future = asyncio.Future()
        await self.queue.put((texts, future))
        return await future

    @get_time
    def embed_documents(self, texts):
        inputs_onnx = self._tokenizer(texts, padding=True, truncation=True, max_length=LOCAL_EMBED_MAX_LENGTH,
                                      return_tensors=self.return_tensors)
        inputs_onnx = {k: v for k, v in inputs_onnx.items()}

        # start_time = time.time()
        outputs_onnx = self.session.run(output_names=['output'], input_feed=inputs_onnx)
        # debug_logger.info(f"onnx infer time: {time.time() - start_time}")

        embedding = outputs_onnx[0][:, 0]
        debug_logger.info(f'embedding shape: {embedding.shape}')

        norm_arr = np.linalg.norm(embedding, axis=1, keepdims=True)
        embeddings_normalized = embedding / norm_arr

        return embeddings_normalized.tolist()

    async def process_queue(self):
        while True:
            batch_texts = []
            futures = []

            try:
                while len(batch_texts) < self.batch_size:
                    texts, future = await asyncio.wait_for(self.queue.get(), timeout=1)
                    batch_texts.extend(texts)
                    futures.append((future, len(texts)))
            except asyncio.TimeoutError:
                pass

            if batch_texts:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(self.executor, self.embed_documents, batch_texts)

                start = 0
                for future, text_count in futures:
                    end = start + text_count
                    future.set_result(result[start:end])
                    start = end