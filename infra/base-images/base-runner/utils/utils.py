class hashdict(dict):
    def __hash__(self):
        return hash(tuple(sorted(self.items())))


class hashlist(list):
    def __hash__(self):
        return hash(tuple(el for el in self))
