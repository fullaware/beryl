import random

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel

ollama_model = OpenAIModel(
    model_name='llama3.1:8b',  
    base_url='http://10.28.28.20:11434/v1',  
)

agent = Agent(
    ollama_model,  
    deps_type=str,  
    system_prompt=(
        "You're a dice game, you should roll the die and see if the number "
        "you get back matches the user's guess. If so, tell them they're a winner. "
        "Use the player's name in the response."
    ),
)


@agent.tool_plain  
def roll_die() -> str:
    """Roll a six-sided die and return the result."""
    return str(random.randint(1, 6))


@agent.tool  
def get_player_name(ctx: RunContext[str]) -> str:
    """Get the player's name."""
    return ctx.deps


dice_result = agent.run_sync('My guess is 4', deps='Anne')  
print(dice_result.data)
#> Congratulations Anne, you guessed correctly! You're a winner!
