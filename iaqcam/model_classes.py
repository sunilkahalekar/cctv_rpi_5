"""Which class ids of a YOLO model are 'person'. Works for the COCO models (yolo26*.pt)
and for a custom-trained model, whatever its class list is."""


def person_class_ids(model_names, wanted):
    """model_names: the model's {id: name}; wanted: names and/or ids from model.classes.
    Returns (ids, note). Raises ValueError when nothing matches."""
    names = {int(k): str(v) for k, v in dict(model_names).items()}
    by_name = {v.strip().lower(): k for k, v in names.items()}
    ids, missing = [], []
    for w in wanted:
        if isinstance(w, int):
            (ids if w in names else missing).append(w)
        elif w.strip().lower() in by_name:
            ids.append(by_name[w.strip().lower()])
        else:
            missing.append(w)
    if not ids and len(names) == 1:
        only = next(iter(names))
        return [only], f"the model has one class ('{names[only]}'): used as person"
    if not ids:
        raise ValueError(f"none of model.classes {list(wanted)} is a class of this model; "
                         f"its classes are: {', '.join(f'{k}={v}' for k, v in sorted(names.items()))}")
    note = f"person class: {', '.join(f'{i}={names[i]}' for i in ids)}"
    if missing:
        note += f" (not in the model: {missing})"
    return sorted(set(ids)), note
