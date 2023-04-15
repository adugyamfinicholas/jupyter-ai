from enum import Enum
import os
import time
from uuid import uuid4
from jupyter_ai.models import AgentChatMessage, ChatMessage, HumanChatMessage
from jupyter_ai_magics.providers import ChatOpenAINewProvider
from langchain import ConversationChain, OpenAI
from langchain.chains import ConversationalRetrievalChain
import ray
from ray.util.queue import Queue
from langchain.memory import ConversationBufferMemory
from langchain.prompts import (
    ChatPromptTemplate, 
    MessagesPlaceholder, 
    SystemMessagePromptTemplate, 
    HumanMessagePromptTemplate
)
from langchain.vectorstores import FAISS
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.document_loaders import DirectoryLoader, TextLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from jupyter_core.paths import jupyter_data_dir

class ACTOR_TYPE(str, Enum):
    DEFAULT = "default"
    FILESYSTEM = "filesystem"
    READ = 'read'

COMMANDS = {
    '/fs': ACTOR_TYPE.FILESYSTEM,
    '/read': ACTOR_TYPE.READ
}

@ray.remote
class Router():
    """Routes messages to the correct actor. To register 
    new actors, add the actor type in the `ACTOR_TYPE` 
    enum and then add a corresponding command in the 
    `COMMANDS` dictionary.
    """

    def __init__(self, log):
        self.log = log

    def route_message(self, message):
        
        # assign default actor
        actor = ray.get_actor(ACTOR_TYPE.DEFAULT)

        if(message.body.startswith("/")):
            command = message.body.split(' ', 1)[0]
            if command in COMMANDS.keys():
                actor = ray.get_actor(COMMANDS[command].value)
        
        actor.process_message.remote(message)


@ray.remote
class DocumentIndexActor():
    def __init__(self, reply_queue: Queue, root_dir: str, log):
        self.reply_queue = reply_queue
        self.root_dir = root_dir
        self.log = log
        self.index_save_dir = os.path.join(jupyter_data_dir(), '.jupyter_ai_indexes')

        if ChatOpenAINewProvider.auth_strategy.name not in os.environ:
            return
        
        if not os.path.exists(self.index_save_dir):
            os.makedirs(self.index_save_dir)

        embeddings = OpenAIEmbeddings()
        try:
            self.index = FAISS.load_local(self.index_save_dir, embeddings)
        except Exception as e:
            self.index = FAISS.from_texts(["This is just starter text for the index"], embeddings)

    def get_index(self):
        return self.index

    def process_message(self, message: HumanChatMessage):
        dir_path = message.body.split(' ', 1)[-1]
        load_path = os.path.join(self.root_dir, dir_path)
        loader = DirectoryLoader(
            load_path, 
            glob="**/*.txt",
            loader_cls=TextLoader
        )
        documents = loader.load_and_split(
            text_splitter=RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=20)
        )
        self.index.add_documents(documents)
        self.index.save_local(self.index_save_dir)

        response = f"""🎉 I have read documents at **{dir_path}** and ready to answer questions. 
        You can ask questions from these docs by prefixing your message with **/fs**."""
        agent_message = AgentChatMessage(
                id=uuid4().hex,
                time=time.time(),
                body=response,
                reply_to=message.id
            )
        self.reply_queue.put(agent_message)


@ray.remote
class FileSystemActor():
    """Processes messages prefixed with /fs. This actor will
    send the message as input to a RetrieverQA chain, that
    follows the Retrieval and Generation (RAG) tehnique to
    query the documents from the index, and sends this context
    to the LLM to generate the final reply.
    """

    def __init__(self, reply_queue: Queue):
        self.reply_queue = reply_queue
        index_actor = ray.get_actor(ACTOR_TYPE.READ.value)
        handle = index_actor.get_index.remote()
        vectorstore = ray.get(handle)
        if not vectorstore:
            return

        self.chat_history = []
        self.chat_provider = ConversationalRetrievalChain.from_llm(
            OpenAI(temperature=0, verbose=True),
            vectorstore.as_retriever()
        )

    def process_message(self, message: HumanChatMessage):
        query = message.body.split(' ', 1)[-1]
        
        index_actor = ray.get_actor(ACTOR_TYPE.READ.value)
        handle = index_actor.get_index.remote()
        vectorstore = ray.get(handle)
        # Have to reference the latest index
        self.chat_provider.retriever = vectorstore.as_retriever()
        
        result = self.chat_provider({"question": query, "chat_history": self.chat_history})
        reply = result['answer']
        self.chat_history.append((query, reply)) 
        agent_message = AgentChatMessage(
            id=uuid4().hex,
            time=time.time(),
            body=reply,
            reply_to=message.id
        )
        self.reply_queue.put(agent_message)

@ray.remote
class DefaultActor():
    def __init__(self, reply_queue: Queue):
        # TODO: Should take the provider/model id as strings
        
        self.reply_queue = reply_queue
        if ChatOpenAINewProvider.auth_strategy.name in os.environ:
            provider = ChatOpenAINewProvider(model_id="gpt-3.5-turbo")
            
            # Create a conversation memory
            memory = ConversationBufferMemory(return_messages=True)
            prompt_template = ChatPromptTemplate.from_messages([
                SystemMessagePromptTemplate.from_template("The following is a friendly conversation between a human and an AI. The AI is talkative and provides lots of specific details from its context. If the AI does not know the answer to a question, it truthfully says it does not know."),
                MessagesPlaceholder(variable_name="history"),
                HumanMessagePromptTemplate.from_template("{input}")
            ])
            chain = ConversationChain(
                llm=provider, 
                prompt=prompt_template,
                verbose=True, 
                memory=memory
            )
            self.chat_provider = chain

    def process_message(self, message: HumanChatMessage):
        if self.chat_provider:
            response = self.chat_provider.predict(input=message.body)
            agent_message = AgentChatMessage(
                id=uuid4().hex,
                time=time.time(),
                body=response,
                reply_to=message.id
            )
            self.reply_queue.put(agent_message)



