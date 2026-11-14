import interactions


class ComponentsV2ModalContext(interactions.ModalContext):
    @classmethod
    def from_dict(cls, client, payload):
        instance = super(interactions.ModalContext, cls).from_dict(client, payload)
        responses = {}
        for container in payload["data"]["components"]:
            component = container.get("component") or container["components"][0]
            value = component.get("values", component.get("value"))
            responses[component["custom_id"]] = value

        instance.responses = responses
        instance.kwargs = responses
        instance.custom_id = payload["data"]["custom_id"]
        instance.edit_origin = False
        return instance
