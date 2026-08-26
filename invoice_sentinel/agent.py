from google.adk.agents.llm_agent import Agent

from .config import MODEL_ID

root_agent = Agent(
    model=MODEL_ID,
    name='root_agent',
    description='A helpful assistant for user questions.',
    instruction='Answer user questions to the best of your knowledge',
)
