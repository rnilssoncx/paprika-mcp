"""A stand-in for paprika_recipes' Remote: recipes, categories, uploads and
notifies, recorded rather than sent. Nothing here touches the network."""

import copy
import gzip
import json

from paprika_recipes.remote import RemoteRecipe


class Response:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class RecipeRemote:
    def __init__(self, recipes=(), categories=()):
        self._recipes = list(recipes)
        # Mutable on purpose: a test changes it between calls to play a member
        # adding a category in the app while the server runs.
        self.categories = list(categories)
        self.category_error = None
        self.category_gets = 0
        # Category writes, as the real API takes them: each POST is recorded as
        # sent (decoded strictly, so a body that is not a gzip-compressed JSON
        # array fails the test), and applied by uid — a whole object replaces
        # the one with its uid, `deleted: true` removes it. Tests assert the
        # recorded batches, not the list afterwards, which would pass for any
        # payload that happens to leave the same list.
        self.category_posts = []
        # The recipe list's body, when a test needs Paprika to answer oddly.
        self.recipe_list_body = None
        self.category_post_result = True
        self.category_post_error = None
        # The meal planner, for the export (meal_plan's tests script their own).
        self.meals = []
        self.uploaded, self.notified, self.notify_error = [], 0, None

    @property
    def recipes(self):
        # A fresh copy each time, as the real Remote builds new objects per
        # call: an edit must reach the upload, not linger in the fake.
        for r in self._recipes:
            yield RemoteRecipe(**r.as_dict())

    def get_recipe_by_id(self, uid, hash):
        for r in self._recipes:
            if r.uid == uid:
                return RemoteRecipe(**r.as_dict())
        raise KeyError(uid)

    def _request(self, method, path, **kwargs):
        if (method, path) == ("get", "/api/v2/sync/recipes/"):
            if self.recipe_list_body is not None:
                return Response(self.recipe_list_body)
            return Response({"result": [{"uid": r.uid, "hash": r.hash} for r in self._recipes]})
        if (method, path) == ("get", "/api/v2/sync/meals/"):
            return Response({"result": list(self.meals)})
        if (method, path) == ("post", "/api/v2/sync/categories/"):
            return self._post_categories(kwargs["files"]["data"])
        assert (method, path) == ("get", "/api/v2/sync/categories/"), (method, path)
        self.category_gets += 1
        if self.category_error:
            raise self.category_error
        # Deep copies, as the real GET is fresh JSON: a decide function that
        # changed an object in place would otherwise change the fixture too.
        return Response({"result": copy.deepcopy(self.categories)})

    def _post_categories(self, part):
        filename, body = part
        assert filename == "data"
        batch = json.loads(gzip.decompress(body))
        assert isinstance(batch, list) and all(isinstance(o, dict) and "uid" in o for o in batch), batch
        self.category_posts.append(batch)
        if self.category_post_error:
            raise self.category_post_error
        if self.category_post_result is True:
            for obj in batch:
                self.categories = [c for c in self.categories if c["uid"] != obj["uid"]]
                if not obj.get("deleted"):
                    self.categories.append(obj)
        return Response({"result": self.category_post_result})

    def upload_recipe(self, recipe):
        self.uploaded.append(recipe.as_dict())
        return recipe

    def notify(self):
        if self.notify_error:
            raise self.notify_error
        self.notified += 1


def category(uid, name, parent=None, order_flag=0, cookbook_uid=None):
    # The five keys the GET returns for every category in the real account.
    return {"uid": uid, "name": name, "order_flag": order_flag, "parent_uid": parent, "cookbook_uid": cookbook_uid}
