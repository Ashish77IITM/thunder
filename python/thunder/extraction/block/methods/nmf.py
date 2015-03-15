from thunder.extraction.block.base import BlockMethod, BlockAlgorithm


class BlockNMF(BlockMethod):

    def __init__(self, **kwargs):
        algorithm = BlockNMFAlgorithm(**kwargs)
        super(self.__class__, self).__init__(algorithm, **kwargs)


class BlockNMFAlgorithm(BlockAlgorithm):

    def __init__(self, threshold=10, **extra):
        self.threshold = threshold

    def extract(self, block):
        return block.mean()
