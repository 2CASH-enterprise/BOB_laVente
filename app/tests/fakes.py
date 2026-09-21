from app.agents.llm_client import LLMClient


class FakeLLMClient(LLMClient):
    """
    Rejoue une séquence de réponses fournie à l'avance, un appel = une réponse de la liste.
    Permet de tester la boucle d'outils de l'orchestrateur de façon déterministe.
    """

    def __init__(self, scripted_responses: list[dict]):
        self.scripted_responses = scripted_responses
        self.call_count = 0
        self.received_messages: list[list[dict]] = []
        self.received_systems: list[str] = []

    async def create_message(self, *, system, messages, tools, max_tokens=1024):
        self.received_messages.append(messages)
        self.received_systems.append(system)
        response = self.scripted_responses[self.call_count]
        self.call_count += 1
        return response


def text_response(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def tool_use_response(tool_name: str, tool_input: dict, tool_use_id: str = "toolu_test") -> dict:
    return {
        "content": [{"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input}],
        "stop_reason": "tool_use",
    }
