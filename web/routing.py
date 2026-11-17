"""web.routing -- collecteur de routes qui GARDE les noms d'endpoints d'origine.

Pourquoi pas des Blueprints ? Un Blueprint préfixe chaque endpoint (`dashboard.dash_home`)
alors que les templates et main.py font `url_for('dash_home')`, `url_for('dash_guild_page')`...
(~65 appels). Ici on enregistre sur l'app avec `add_url_rule(endpoint=<nom de la fonction>)` :
exactement ce que fait `@api.route(...)`, donc zéro changement dans les templates.

    routes = Routes()

    @routes.route("/login")
    def dash_login(): ...

    # dans main.py, après la création de `api` :
    routes.register(api)
"""


class Routes:
    def __init__(self):
        self._rules = []

    def route(self, rule, **options):
        def decorator(view):
            self._rules.append((rule, view, options))
            return view
        return decorator

    def register(self, app):
        for rule, view, options in self._rules:
            app.add_url_rule(rule, endpoint=view.__name__, view_func=view, **dict(options))
