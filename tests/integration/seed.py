# Runs inside `odoo shell`: creates two users with different rights and an API key each.
# Prints one JSON line with the keys and record ids for the integration tests.
import datetime
import json

env = env  # noqa: F821 - provided by `odoo shell`

expires = datetime.datetime.now() + datetime.timedelta(days=1)
germany = env.ref("base.de")
shared = env["res.partner"].create(
    {"name": "MCP Shared Partner", "ref": "mcp-shared", "country_id": germany.id}
)
secret = env["res.partner"].create({"name": "MCP Alice Only", "ref": "mcp-alice-only"})

# Odoo 20 models access as `ir.access`: a group-bound record grants a permission
# limited to its domain. Bob (portal) may read every partner except alice-only ones.
restricted = env["res.groups"].create({"name": "MCP Restricted Readers"})
env["ir.access"].create(
    {
        "name": "MCP restricted readers: partners except alice-only",
        "model_id": env.ref("base.model_res_partner").id,
        "group_id": restricted.id,
        "operation": "r",
        "domain": "[('ref', '!=', 'mcp-alice-only')]",
    }
)

alice = env["res.users"].create(
    {"name": "Alice", "login": "alice", "group_ids": [(6, 0, [env.ref("base.group_user").id])]}
)
bob = env["res.users"].create(
    {
        "name": "Bob",
        "login": "bob",
        "group_ids": [(6, 0, [env.ref("base.group_portal").id, restricted.id])],
    }
)

keys = {
    login: env["res.users.apikeys"].with_user(user)._generate("rpc", "mcp-integration", expires)
    for login, user in (("alice", alice), ("bob", bob))
}
env.cr.commit()
print("SEED_RESULT " + json.dumps({"keys": keys, "shared_id": shared.id, "secret_id": secret.id}))
