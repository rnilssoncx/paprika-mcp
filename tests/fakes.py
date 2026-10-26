"""A stand-in for paprika_recipes' Remote: recipes, categories, uploads and
notifies, recorded rather than sent. Nothing here touches the network."""

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
        self.uploaded, self.notified, self.notify_error = [], 0, None

    @property
    def recipes(self):
        # A fresh copy each time, as the real Remote builds new objects per
        # call: an edit must reach the upload, not linger in the fake.
        for r in self._recipes:
            yield RemoteRecipe(**r.as_dict())

    def _request(self, method, path, **kwargs):
        assert (method, path) == ("get", "/api/v2/sync/categories/"), (method, path)
        self.category_gets += 1
        if self.category_error:
            raise self.category_error
        return Response({"result": list(self.categories)})

    def upload_recipe(self, recipe):
        self.uploaded.append(recipe.as_dict())
        return recipe

    def notify(self):
        if self.notify_error:
            raise self.notify_error
        self.notified += 1


def category(uid, name, parent=None):
    return {"uid": uid, "name": name, "parent_uid": parent}
