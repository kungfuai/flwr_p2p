from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Any
from tensorflow import keras
from wandb.keras import WandbCallback

from flwr.common import ndarrays_to_parameters


from flwr.server.strategy import (
    FedAvg,
    FedAdam,
    FedAvgM,
    FedOpt,
    FedYogi,
    FedAdagrad,
    FedMedian,
    QFedAvg,
)


from flwr_p2p.federated_node.async_federated_node import AsyncFederatedNode
from flwr_p2p.federated_node.sync_federated_node import SyncFederatedNode
from flwr_p2p.shared_folder.in_memory_folder import InMemoryFolder
from flwr_p2p.keras.federated_learning_callback import FlwrFederatedCallback


from experiments.base_experiment_runner import BaseExperimentRunner
from experiments.custom_wandb_callback import CustomWandbCallback


class FederatedLearningRunner(BaseExperimentRunner):
    def __init__(
        self,
        config,
        **kwargs,
    ):
        super().__init__(config, **kwargs)
        self.storage_backend: Any = InMemoryFolder()
        # In one round, each node trains on its local data for one epoch.
        self.num_rounds = self.epochs  # number of federated rounds (similar to epochs)

    def run(self):
        print("Net:", self.net)
        self.models = self.create_models()
        self.set_strategy()
        (
            self.partitioned_x_train,
            self.partitioned_y_train,
            self.x_test,
            self.y_test,
        ) = self.split_data()
        self.train_federated_models()
        self.evaluate()

    def set_strategy(self):
        if self.strategy_name == "fedavg":
            self.strategies = [FedAvg() for _ in range(self.num_nodes)]
        elif self.strategy_name == "fedavgm":
            self.strategies = [FedAvgM() for _ in range(self.num_nodes)]
        elif self.strategy_name == "fedadam":
            self.strategies = [
                FedAdam(
                    initial_parameters=ndarrays_to_parameters(
                        self.models[i].get_weights()
                    )
                )
                for i in range(self.num_nodes)
            ]
        elif self.strategy_name == "fedopt":
            self.strategies = [
                FedOpt(
                    initial_parameters=ndarrays_to_parameters(
                        self.models[i].get_weights()
                    )
                )
                for i in range(self.num_nodes)
            ]
        elif self.strategy_name == "fedmedian":
            self.strategies = [FedMedian() for _ in range(self.num_nodes)]
        # elif self.strategy_name == "fedyogi":
        #     self.strategy = FedYogi()
        # elif self.strategy_name == "fedadagrad":
        #     self.strategy = FedAdagrad()
        else:
            raise ValueError("Strategy not supported")

    def split_data(self):
        if self.data_split == "random":
            return self.random_split()
        elif self.data_split == "partitioned":
            return self.create_partitioned_datasets()
        elif self.data_split == "skewed":
            return self.create_skewed_partition_split()
        else:
            raise ValueError("Data split not supported")

    def train_federated_models(
        self,
    ) -> List[keras.Model]:
        if self.federated_type == "pseudo-concurrent":
            print("Training federated models pseudo-concurrently.")
            return self._train_federated_models_pseudo_concurrently(self.models)
        elif self.federated_type == "concurrent":  # should be used for all experiments
            print("Training federated models concurrently")
            return self._train_federated_models_concurrently(self.models)
        else:
            print("Training federated models sequentially")
            return self._train_federated_models_sequentially(self.models)

    def _train_federated_models_concurrently(
        self, model_federated: List[keras.Model]
    ) -> List[keras.Model]:
        nodes = self.create_nodes()
        num_partitions = self.num_nodes

        callbacks_per_client = [
            FlwrFederatedCallback(
                nodes[i],
                x_test=self.x_test,
                y_test=self.y_test,
                num_examples_per_epoch=self.steps_per_epoch * self.batch_size,
            )
            for i in range(num_partitions)
        ]

        train_loaders = [
            self.get_train_dataloader_for_node(i) for i in range(num_partitions)
        ]

        with ThreadPoolExecutor(max_workers=self.num_nodes) as ex:
            futures = []
            for i_node in range(self.num_nodes):
                callbacks = [
                    callbacks_per_client[i_node],
                ]
                if self.tracking:
                    callbacks.append(CustomWandbCallback(i_node))

                # assert self.test_steps is None
                future = ex.submit(
                    model_federated[i_node].fit,
                    x=train_loaders[i_node],
                    epochs=self.num_rounds,
                    steps_per_epoch=self.steps_per_epoch,
                    callbacks=callbacks,
                    validation_data=(self.x_test, self.y_test),
                    validation_steps=self.test_steps,
                    validation_batch_size=self.batch_size,
                )
                futures.append(future)

            train_results = []
            for future in as_completed(futures):
                train_results.append(future.result())

        return model_federated

    def _train_federated_models_pseudo_concurrently(
        self, model_federated: List[keras.Model]
    ) -> List[keras.Model]:
        self.lag = 0.1
        nodes = self.create_nodes()
        num_partitions = self.num_nodes
        if self.test_steps is None:
            x_test = self.x_test
            y_test = self.y_test
        else:
            x_test = self.x_test[: self.test_steps * self.batch_size, ...]
            y_test = self.y_test[: self.test_steps * self.batch_size, ...]

        callbacks_per_client = [
            FlwrFederatedCallback(
                nodes[i],
                num_examples_per_epoch=self.steps_per_epoch * self.batch_size,
                x_test=x_test,
                y_test=y_test,
                # x_test=self.x_test[: self.test_steps * self.batch_size, ...],
                # y_test=self.y_test[: self.test_steps * self.batch_size, ...],
            )
            for i in range(num_partitions)
        ]

        num_federated_rounds = self.num_rounds
        num_epochs_per_round = 1
        train_loaders = [
            self.get_train_dataloader_for_node(i) for i in range(num_partitions)
        ]

        seqs = [[]] * self.num_nodes
        for i_node in range(self.num_nodes):
            seqs[i_node] = [
                (i_node, j + i_node * self.lag) for j in range(num_federated_rounds)
            ]
        # mix them up
        execution_sequence = []
        for i_node in range(self.num_nodes):
            execution_sequence.extend(seqs[i_node])
        execution_sequence = [
            x[0] for x in sorted(execution_sequence, key=lambda x: x[1])
        ]
        print(f"Execution sequence: {execution_sequence}")
        if self.test_steps is None:
            x_test = self.x_test
            y_test = self.y_test
        else:
            x_test = self.x_test[: self.test_steps * self.batch_size, ...]
            y_test = self.y_test[: self.test_steps * self.batch_size, ...]
        for i_node in execution_sequence:
            print("Training node", i_node)
            model_federated[i_node].fit(
                x=train_loaders[i_node],
                epochs=num_epochs_per_round,
                steps_per_epoch=self.steps_per_epoch,
                callbacks=[callbacks_per_client[i_node]],
                validation_data=(x_test, y_test),
                validation_steps=self.test_steps,
                validation_batch_size=self.batch_size,
            )

            if i_node == 0:
                print("Evaluating on the combined test set:")
                model_federated[0].evaluate(
                    x_test,
                    y_test,
                    # self.x_test[: self.test_steps * self.batch_size, ...],
                    # self.y_test[: self.test_steps * self.batch_size, ...],
                    batch_size=self.batch_size,
                    steps=10,
                )

        return model_federated

    def _train_federated_models_sequentially(
        self, model_federated: List[keras.Model]
    ) -> List[keras.Model]:
        nodes = self.create_nodes()
        num_partitions = self.num_nodes  # is this needed?

        callbacks_per_client = [
            FlwrFederatedCallback(
                nodes[i], num_examples_per_epoch=self.batch_size * self.steps_per_epoch
            )
            for i in range(num_partitions)
        ]

        num_federated_rounds = self.num_rounds
        num_epochs_per_round = 1
        train_loaders = [
            self.get_train_dataloader_for_node(i) for i in range(num_partitions)
        ]

        if self.tracking:
            wandb_callbacks = [WandbCallback() for i in range(num_partitions)]
        for i_round in range(num_federated_rounds):
            print("\n============ Round", i_round)
            callbacks = [
                callbacks_per_client[i_partition],
            ]
            if self.tracking:
                callbacks.append(wandb_callbacks[i_partition])
            for i_partition in range(num_partitions):
                model_federated[i_partition].fit(
                    train_loaders[i_partition],
                    epochs=num_epochs_per_round,
                    steps_per_epoch=self.steps_per_epoch,
                    callbacks=callbacks,
                )
            print("Evaluating on the combined test set:")
            model_federated[0].evaluate(
                self.x_test,
                self.y_test,
                batch_size=self.batch_size,
                steps=self.steps_per_epoch,
            )

        return model_federated

    def create_nodes(self):
        if self.use_async:
            nodes = [
                AsyncFederatedNode(
                    shared_folder=self.storage_backend, strategy=self.strategies[i]
                )
                for i in range(self.num_nodes)
            ]
        else:
            nodes = [
                SyncFederatedNode(
                    shared_folder=self.storage_backend,
                    strategy=self.strategies[i],
                    num_nodes=self.num_nodes,
                )
                for i in range(self.num_nodes)
            ]
        return nodes

    def evaluate(self):
        for i_node in range(self.num_nodes):
            loss1, accuracy1 = self.models[i_node].evaluate(
                self.x_test,
                self.y_test,
                batch_size=self.batch_size,
                steps=self.test_steps,
            )
