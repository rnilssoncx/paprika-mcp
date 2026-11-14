"""Tools module - exports all MCP tool implementations."""

from ..upload_probe import TOOL_DEFINITION as PHOTO_PROBE_DEF  # THROWAWAY: 053 S3
from ..upload_probe import link_tool as photo_probe_link_tool  # THROWAWAY: 053 S3
from .categories import (
    CREATE_CATEGORY_DEF,
    DELETE_CATEGORY_DEF,
    MOVE_CATEGORY_DEF,
    RENAME_CATEGORY_DEF,
    create_category_tool,
    delete_category_tool,
    move_category_tool,
    rename_category_tool,
)
from .create_recipe import TOOL_DEFINITION as CREATE_RECIPE_DEF
from .create_recipe import create_recipe_tool
from .format_fraction import TOOL_DEFINITION as FORMAT_FRACTION_DEF
from .format_fraction import format_fraction_tool
from .list_categories import TOOL_DEFINITION as LIST_CATEGORIES_DEF
from .list_categories import list_categories_tool
from .meal_plan import (
    ADD_MEAL_DEF,
    LIST_MEALS_DEF,
    LIST_MENUS_DEF,
    PLAN_MENU_DEF,
    REMOVE_MEAL_DEF,
    add_meal_tool,
    list_meals_tool,
    list_menus_tool,
    plan_menu_tool,
    remove_meal_tool,
)
from .read_recipe import TOOL_DEFINITION as READ_RECIPE_DEF
from .read_recipe import read_recipe_tool
from .restore_recipe import TOOL_DEFINITION as RESTORE_RECIPE_DEF
from .restore_recipe import restore_recipe_tool
from .search_recipes import TOOL_DEFINITION as SEARCH_RECIPES_DEF
from .search_recipes import search_recipes_tool
from .update_recipe import TOOL_DEFINITION as UPDATE_RECIPE_DEF
from .update_recipe import update_recipe_tool

# Export all tools and their definitions
TOOLS = {
    "search_recipes": {
        "definition": SEARCH_RECIPES_DEF,
        "handler": search_recipes_tool,
    },
    "read_recipe": {
        "definition": READ_RECIPE_DEF,
        "handler": read_recipe_tool,
    },
    "update_recipe": {
        "definition": UPDATE_RECIPE_DEF,
        "handler": update_recipe_tool,
    },
    "create_recipe": {
        "definition": CREATE_RECIPE_DEF,
        "handler": create_recipe_tool,
    },
    "create_category": {"definition": CREATE_CATEGORY_DEF, "handler": create_category_tool},
    "rename_category": {"definition": RENAME_CATEGORY_DEF, "handler": rename_category_tool},
    "move_category": {"definition": MOVE_CATEGORY_DEF, "handler": move_category_tool},
    "delete_category": {"definition": DELETE_CATEGORY_DEF, "handler": delete_category_tool},
    "restore_recipe": {
        "definition": RESTORE_RECIPE_DEF,
        "handler": restore_recipe_tool,
    },
    "list_categories": {
        "definition": LIST_CATEGORIES_DEF,
        "handler": list_categories_tool,
    },
    "format_fraction": {
        "definition": FORMAT_FRACTION_DEF,
        "handler": format_fraction_tool,
    },
    "list_meals": {"definition": LIST_MEALS_DEF, "handler": list_meals_tool},
    "add_meal": {"definition": ADD_MEAL_DEF, "handler": add_meal_tool},
    "remove_meal": {"definition": REMOVE_MEAL_DEF, "handler": remove_meal_tool},
    "list_menus": {"definition": LIST_MENUS_DEF, "handler": list_menus_tool},
    "plan_menu": {"definition": PLAN_MENU_DEF, "handler": plan_menu_tool},
    "photo_probe_link": {"definition": PHOTO_PROBE_DEF, "handler": photo_probe_link_tool},  # THROWAWAY: 053 S3
}

__all__ = [
    "TOOLS",
    "search_recipes_tool",
    "read_recipe_tool",
    "update_recipe_tool",
    "restore_recipe_tool",
    "create_recipe_tool",
    "create_category_tool",
    "rename_category_tool",
    "move_category_tool",
    "delete_category_tool",
    "list_categories_tool",
    "format_fraction_tool",
]
