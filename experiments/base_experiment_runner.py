import numpy as np

# from flwr_p2p.keras.example import MnistModelBuilder
from experiments.simple_mnist_model import SimpleMnistModel
from experiments.model.keras_models import ResNetModelBuilder


class BaseExperimentRunner:
    def __init__(self, config, tracking=False):
        self.config = config
        self.num_nodes = config["num_nodes"]
        self.batch_size = config["batch_size"]
        self.epochs = config["epochs"]
        self.steps_per_epoch = config["steps_per_epoch"]
        self.lr = config["lr"]
        # In experiment tracking, log the actual test steps and test data size
        self.test_steps = config.get("test_steps", None)
        self.use_async = config["use_async"]
        self.federated_type = config["federated_type"]
        self.strategy_name = config["strategy"]
        self.data_split = config["data_split"]
        self.dataset = config["dataset"]
        self.net = config["net"]

        self.tracking = tracking

        self.get_original_data()

    # ***currently works only for mnist***
    def create_models(self):
        if self.dataset == "mnist":
            assert self.net == "simple", f"Net not supported: {self.net} for mnist"
        if self.net == "simple":
            return [SimpleMnistModel(lr=self.lr).run() for _ in range(self.num_nodes)]
        elif self.net == "resnet50":
            return [
                ResNetModelBuilder(lr=self.lr, net="ResNet50", weights="imagenet").run()
                for _ in range(self.num_nodes)
            ]

    def get_original_data(self):
        dataset = self.dataset
        if dataset == "mnist":
            from tensorflow.keras.datasets import mnist

            (self.x_train, self.y_train), (self.x_test, self.y_test) = mnist.load_data()
        elif dataset == "cifar10":
            from tensorflow.keras.datasets import cifar10

            (self.x_train, self.y_train), (
                self.x_test,
                self.y_test,
            ) = cifar10.load_data()

    def normalize_data(self, data):
        image_size = data.shape[1]
        if self.dataset == "mnist":
            reshaped_data = np.reshape(data, [-1, image_size, image_size, 1])
        elif self.dataset == "cifar10":
            reshaped_data = np.reshape(data, [-1, image_size, image_size, 3])
        else:
            raise ValueError(f"Dataset not supported: {self.dataset}")
        normalized_data = reshaped_data.astype(np.float32) / 255
        return normalized_data

    def random_split(self):
        num_partitions = self.num_nodes
        x_train = self.normalize_data(self.x_train)
        x_test = self.normalize_data(self.x_test)

        # shuffle data then partition
        num_train = x_train.shape[0]
        indices = np.random.permutation(num_train)
        x_train = x_train[indices]
        y_train = self.y_train[indices]

        partitioned_x_train = np.array_split(x_train, num_partitions)
        partitioned_y_train = np.array_split(y_train, num_partitions)

        return partitioned_x_train, partitioned_y_train, x_test, self.y_test

    def create_skewed_partition_split(self, skew_factor: float = 0.90):
        # only works for 2 nodes at the moment
        # returns a "skewed" partition of data
        # Ex: 0.8 means 80% of the data for one node is 0-4 while 20% is 5-9
        # and vice versa for the other node
        # Note: A skew factor 0f 0.5 would essentially be a random split,
        # and 1 would be like a normal partition

        # num_partitions = self.num_nodes
        x_train = self.normalize_data(self.x_train)
        x_test = self.normalize_data(self.x_test)

        x_train_by_label = [[] for _ in range(10)]
        y_train_by_label = [[] for _ in range(10)]
        for i in range(len(self.y_train)):
            label = self.y_train[i]
            x_train_by_label[label].append(x_train[i])
            y_train_by_label[label].append(label)

        skewed_partitioned_x_train = [[], []]
        skewed_partitioned_y_train = [[], []]
        for i in range(10):
            num_samples = len(x_train_by_label[i])
            num_samples_for_node_1 = int(num_samples * skew_factor)
            skewed_partitioned_x_train[int(i / 5)].extend(
                x_train_by_label[i][:num_samples_for_node_1]
            )
            skewed_partitioned_y_train[int(i / 5)].extend(
                y_train_by_label[i][:num_samples_for_node_1]
            )
            skewed_partitioned_x_train[int((i / 5)) - 1].extend(
                x_train_by_label[i][num_samples_for_node_1:]
            )
            skewed_partitioned_y_train[int((i / 5)) - 1].extend(
                y_train_by_label[i][num_samples_for_node_1:]
            )

        # convert to numpy arrays
        skewed_partitioned_x_train[0] = np.asarray(skewed_partitioned_x_train[0])
        skewed_partitioned_x_train[1] = np.asarray(skewed_partitioned_x_train[1])
        skewed_partitioned_y_train[0] = np.asarray(skewed_partitioned_y_train[0])
        skewed_partitioned_y_train[1] = np.asarray(skewed_partitioned_y_train[1])

        # shuffle data
        for i in range(2):
            num_train = skewed_partitioned_x_train[i].shape[0]
            indices = np.random.permutation(num_train)
            skewed_partitioned_x_train[i] = skewed_partitioned_x_train[i][indices]
            skewed_partitioned_y_train[i] = skewed_partitioned_y_train[i][indices]

        return (
            skewed_partitioned_x_train,
            skewed_partitioned_y_train,
            x_test,
            self.y_test,
        )

    def create_partitioned_datasets(self):
        num_partitions = self.num_nodes

        x_train = self.normalize_data(self.x_train)
        x_test = self.normalize_data(self.x_test)

        (
            partitioned_x_train,
            partitioned_y_train,
        ) = self.split_training_data_into_paritions(
            x_train, self.y_train, num_partitions=num_partitions
        )
        return partitioned_x_train, partitioned_y_train, x_test, self.y_test

    def get_train_dataloader_for_node(self, node_idx: int):
        partition_idx = node_idx
        partitioned_x_train = self.partitioned_x_train
        partitioned_y_train = self.partitioned_y_train
        while True:
            for i in range(0, len(partitioned_x_train[partition_idx]), self.batch_size):
                x_train_batch, y_train_batch = (
                    partitioned_x_train[partition_idx][i : i + self.batch_size],
                    partitioned_y_train[partition_idx][i : i + self.batch_size],
                )
                # print("x_train_batch.shape", x_train_batch.shape)
                # print("y_train_batch.shape", y_train_batch.shape)
                # raise Exception("stop")
                yield x_train_batch, y_train_batch

    # ***currently this only works for mnist*** and for num_nodes = 2, 10
    def split_training_data_into_paritions(
        self, x_train, y_train, num_partitions: int = 2
    ):
        # partion 1: classes 0-4
        # partion 2: classes 5-9
        # client 1 train on classes 0-4 only, and validated on 0-9
        # client 2 train on classes 5-9 only, and validated on 0-9
        # both clients will have low accuracy on 0-9 (below 0.6)
        # but when federated, the accuracy will be higher than 0.6
        classes = list(range(10))
        num_classes_per_partition = int(len(classes) / num_partitions)
        partitioned_classes = [
            classes[i : i + num_classes_per_partition]
            for i in range(0, len(classes), num_classes_per_partition)
        ]
        partitioned_x_train = []
        partitioned_y_train = []
        for partition in partitioned_classes:
            # partition is a list of int
            if len(y_train.shape) == 2:
                selected = np.isin(y_train, partition)[:, 0]
            elif len(y_train.shape) == 1:
                selected = np.isin(y_train, partition)
            # subsetting based on the first axis
            x_train_selected = x_train[selected]
            assert (
                x_train_selected.shape[0] < x_train.shape[0]
            ), "partitioned dataset should be smaller than original dataset"
            assert x_train_selected.shape[0] == y_train[selected].shape[0]
            partitioned_x_train.append(x_train_selected)
            y_train_selected = y_train[selected]
            partitioned_y_train.append(y_train_selected)

        return partitioned_x_train, partitioned_y_train


if __name__ == "__main__":
    config = {
        "epochs": 256,
        "batch_size": 32,
        "steps_per_epoch": 8,
        "lr": 0.001,
        "num_nodes": 2,
    }
    base_exp = BaseExperimentRunner(config, num_nodes=2)

    base_exp.random_split()
    base_exp.create_skewed_partition_split()
