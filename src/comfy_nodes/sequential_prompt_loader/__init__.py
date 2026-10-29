from .sequential_prompt_node import SequentialMarkdownPromptLoader

NODE_CLASS_MAPPINGS = {
    "SequentialMarkdownPromptLoader": SequentialMarkdownPromptLoader
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "SequentialMarkdownPromptLoader": "Sequential Markdown Prompt Loader"
}

WEB_DIRECTORY = "./js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
