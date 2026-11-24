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

# Bob may read every partner except alice-only ones. Odoo 20 models this as one
# group-bound `ir.access` record; Odoo 19 needs an ACL plus a group record rule.
restricted = env["res.groups"].create({"name": "MCP Restricted Readers"})
partner_model = env.ref("base.model_res_partner").id
not_alice_only = "[('ref', '!=', 'mcp-alice-only')]"
if "ir.access" in env:
    env["ir.access"].create(
        {
            "name": "MCP restricted readers: partners except alice-only",
            "model_id": partner_model,
            "group_id": restricted.id,
            "operation": "r",
            "domain": not_alice_only,
        }
    )
else:
    env["ir.model.access"].create(
        {
            "name": "MCP restricted readers: read partners",
            "model_id": partner_model,
            "group_id": restricted.id,
            "perm_read": True,
        }
    )
    env["ir.rule"].create(
        {
            "name": "MCP restricted readers: partners except alice-only",
            "model_id": partner_model,
            "groups": [(6, 0, [restricted.id])],
            "domain_force": not_alice_only,
            "perm_read": True,
            "perm_write": False,
            "perm_create": False,
            "perm_unlink": False,
        }
    )

# Alice: internal user who may also create contacts (needed for the write tests).
alice_groups = [env.ref("base.group_user").id, env.ref("base.group_partner_manager").id]
alice = env["res.users"].create(
    {"name": "Alice", "login": "alice", "group_ids": [(6, 0, alice_groups)]}
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
