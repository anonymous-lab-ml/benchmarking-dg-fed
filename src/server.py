import copy
import logging
from multiprocessing import pool, cpu_count

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import BatchSampler, RandomSampler
from tqdm.auto import tqdm
from collections import OrderedDict


from .models import *
from .utils import *
from .client import *
from .dataset_bundle import *

logger = logging.getLogger(__name__)


class FedAvg(object):
    def __init__(self, seed, exp_id, device, ds_bundle, server_config):
        self.seed = seed
        self.id = exp_id
        self.ds_bundle = ds_bundle
        self.device = device
        self.clients = []
        self.server_config = server_config
        self.mp_flag = server_config['mp']
        self.num_rounds = server_config['num_rounds']
        self.fraction = server_config['fraction']
        self.num_clients = 0
        self.test_dataloader = {}
        self._round = 0
        self.featurizer = None
        self.classifier = None
    
    def setup_model(self, model_file, start_epoch):
        """
        The model setup depends on the datasets. 
        """
        assert self._round == 0
        self._featurizer = self.ds_bundle.featurizer
        self._classifier = self.ds_bundle.classifier
        self.featurizer = nn.DataParallel(self._featurizer)
        self.classifier = nn.DataParallel(self._classifier)
        self.model = nn.DataParallel(nn.Sequential(self._featurizer, self._classifier))
        if model_file:
            self.model.load_state_dict(torch.load(model_file))
            self._round = int(start_epoch)

    def register_clients(self, clients):
        # assert self._round == 0
        self.clients = clients
        self.num_clients = len(self.clients)
        for client in self.clients:
            client.setup_model(copy.deepcopy(self._featurizer), copy.deepcopy(self._classifier))
    
    def register_testloader(self, dataloaders):
        self.test_dataloader.update(dataloaders)
    
    def transmit_model(self, sampled_client_indices=None):
        """
            Description: Send the updated global model to selected/all clients.
            This method could be overriden by the derived class if one algorithm requires to send things other than model parameters.
        """
        if sampled_client_indices is None:
            # send the global model to all clients before the very first and after the last federated round
            for client in tqdm(self.clients, leave=False):
                client.update_model(self.model.state_dict())

            message = f"[Round: {str(self._round).zfill(3)}] ...successfully transmitted models to all {str(self.num_clients)} clients!"
            logging.debug(message)
            del message
        else:
            # send the global model to selected clients
            for idx in tqdm(sampled_client_indices, leave=False):
                self.clients[idx].update_model(self.model.state_dict())
            message = f"[Round: {str(self._round).zfill(3)}] ...successfully transmitted models to {str(len(sampled_client_indices))} selected clients!"
            logging.debug(message)
            del message

    def sample_clients(self):
        """
        Description: Sample a subset of clients. 
        Could be overriden if some methods require specific ways of sampling.
        """
        # sample clients randommly
        message = f"[Round: {str(self._round).zfill(3)}] Select clients...!"
        logging.debug(message)
        del message

        num_sampled_clients = max(int(self.fraction * self.num_clients), 1)
        sampled_client_indices = sorted(np.random.choice(a=[i for i in range(self.num_clients)], size=num_sampled_clients, replace=False).tolist())

        return sampled_client_indices
    

    def update_clients(self, sampled_client_indices):
        """
        Description: This method will call the client.fit methods. 
        Usually doesn't need to override in the derived class.
        """
        def update_single_client(selected_index):
            self.clients[selected_index].fit()
            client_size = len(self.clients[selected_index])
            return client_size

        message = f"[Round: {str(self._round).zfill(3)}] Start updating selected {len(sampled_client_indices)} clients...!"
        logging.debug(message)
        selected_total_size = 0
        if self.mp_flag:
            with pool.ThreadPool(processes=cpu_count() - 1) as workhorse:
                selected_total_size = workhorse.map(update_single_client, sampled_client_indices)
            selected_total_size = sum(selected_total_size)
        else:
            for idx in tqdm(sampled_client_indices, leave=False):
                client_size = update_single_client(idx)
                selected_total_size += client_size
        message = f"[Round: {str(self._round).zfill(3)}] ...{len(sampled_client_indices)} clients are selected and updated (with total sample size: {str(selected_total_size)})!"
        logging.debug(message)
        return selected_total_size


    def evaluate_clients(self, sampled_client_indices):
        def evaluate_single_client(selected_index):
            self.clients[selected_index].client_evaluate()
            return True
        
        message = f"[Round: {str(self._round).zfill(3)}] Evaluate selected {str(len(sampled_client_indices))} clients' models...!"
        logging.debug(message)
        del message
        if self.mp_flag:
            with pool.ThreadPool(processes=cpu_count() - 1) as workhorse:
                workhorse.map(evaluate_single_client, sampled_client_indices)
        else:
            for idx in sampled_client_indices:
                self.clients[idx].client_evaluate()
            

    def aggregate(self, sampled_client_indices, coefficients):
        """Average the updated and transmitted parameters from each selected client."""
        message = f"[Round: {str(self._round).zfill(3)}] Aggregate updated weights of {len(sampled_client_indices)} clients...!"
        logging.debug(message)
        del message

        averaged_weights = OrderedDict()
        for it, idx in tqdm(enumerate(sampled_client_indices), leave=False):
            local_weights = self.clients[idx].model.state_dict()
            for key in self.model.state_dict().keys():
                if it == 0:
                    averaged_weights[key] = coefficients[it] * local_weights[key]
                else:
                    averaged_weights[key] += coefficients[it] * local_weights[key]
        self.model.load_state_dict(averaged_weights)

        message = f"[Round: {str(self._round).zfill(3)}] ...updated weights of {len(sampled_client_indices)} clients are successfully averaged!"
        logging.debug(message)
        del message
    

    def train_federated_model(self):
        """Do federated training."""
        # select pre-defined fraction of clients randomly
        sampled_client_indices = self.sample_clients()

        # send global model to the selected clients
        self.transmit_model(sampled_client_indices)

        # updated selected clients with local dataset
        selected_total_size = self.update_clients(sampled_client_indices)

        # evaluate selected clients with local dataset (same as the one used for local update)
        # self.evaluate_clients(sampled_client_indices)

        # average each updated model parameters of the selected clients and update the global model
        mixing_coefficients = [len(self.clients[idx]) / selected_total_size for idx in sampled_client_indices]
        self.aggregate(sampled_client_indices, mixing_coefficients)
    
    def evaluate_global_model(self, dataloader):
        """Evaluate the global model using the global holdout dataset (self.data)."""
        self.model.eval()
        self.model.to(self.device)

        with torch.no_grad():
            y_pred = None
            y_true = None
            for batch in dataloader:
                data, labels, meta_batch = batch[0], batch[1], batch[2]
                data, labels = data.to(self.device), labels.to(self.device)
                y = self.model(data)
                prediction = torch.argmax(y, dim=-1)
                if y_pred is None:
                    y_pred = prediction
                    y_true = labels
                    metadata = meta_batch
                else:
                    y_pred = torch.cat((y_pred, prediction))
                    y_true = torch.cat((y_true, labels))
                    metadata = torch.cat((metadata, meta_batch))
            metric = self.ds_bundle.dataset.eval(y_pred.to("cpu"), y_true.to("cpu"), metadata.to("cpu"))
            if self.device == "cuda": torch.cuda.empty_cache()
        self.model.to("cpu")
        return metric

    def fit(self):
        """
        Description: Execute the whole process of the federated learning.
        """
        message = f"Round \t "
        for testset_name in self.test_dataloader.keys():
            message += f"{testset_name} \t "
        logging.info(message)
        key_metric = []
        for r in range(self.num_rounds):
            print("num of rounds: {}".format(r))
            key_metric.append([])
            self._round += 1
            self.train_federated_model()
            message = f"{str(self._round).zfill(3)} \t "
            for name, dataloader in self.test_dataloader.items():
                metric = self.evaluate_global_model(dataloader)
                print(metric[1])
                for value in metric[0].values():
                    message += f"{value:05.4} "
                message += f"\t"
                key_metric[-1].append(list(metric[0].values())[-1])
            logging.info(message)
            self.save_model(r)
        key_metric = np.array(key_metric)
        in_max_idx, lodo_max_idx, _, _ = np.argmax(key_metric, axis=0)
        print(f"{key_metric[in_max_idx][2]:05.4} \t {key_metric[in_max_idx][3]:05.4} \t {key_metric[lodo_max_idx][3]:05.4}")
        self.transmit_model()

    def save_model(self, num_epoch):
        path = f"/local/scratch/a/bai116/models/{self.ds_bundle.name}_{self.clients[0].name}_{self.id:04d}_{num_epoch}.pth"
        torch.save(self.model.state_dict(), path)


class FedDG(FedAvg):
    def register_clients(self, clients):
        # assert self._round == 0
        self.clients = clients
        self.num_clients = len(self.clients)
        for client in self.clients:
            client.setup_model(copy.deepcopy(self._featurizer), copy.deepcopy(self._classifier))
            client.set_global_dataloader(self.global_dataloader)
        super().register_clients(clients)
            
    def set_global_dataloader(self, global_dataset):
        self.global_dataloader = global_dataset

class FedADG(FedAvg):
    def setup_model(self, model_file, start_epoch):
        """
        The model setup depends on the datasets. 
        """
        assert self._round == 0
        self._featurizer = self.ds_bundle.featurizer
        self._classifier = self.ds_bundle.classifier
        self._generator = GeneDistrNet(num_labels=self.ds_bundle.n_classes)
        self.featurizer = nn.DataParallel(self._featurizer)
        self.classifier = nn.DataParallel(self._classifier)
        self.generator = nn.DataParalle(self._generator)
        self.model = nn.DataParallel(nn.Sequential(self._featurizer, self._classifier))
        if model_file:
            self.model.load_state_dict(torch.load(model_file))
            self._round = int(start_epoch)

    def register_clients(self, clients):
        # assert self._round == 0
        self.clients = clients
        self.num_clients = len(self.clients)
        for client in self.clients:
            client.setup_model(copy.deepcopy(self._featurizer), copy.deepcopy(self._classifier), copy.deepcopy(self._generator))

    def transmit_model(self, sampled_client_indices=None):
        """
            Description: Send the updated global model to selected/all clients.
            This method could be overriden by the derived class if one algorithm requires to send things other than model parameters.
        """
        if sampled_client_indices is None:
            # send the global model to all clients before the very first and after the last federated round
            for client in tqdm(self.clients, leave=False):
                client.update_model(self.model.state_dict(), self._generator.state_dict())

            message = f"[Round: {str(self._round).zfill(3)}] ...successfully transmitted models to all {str(self.num_clients)} clients!"
            logging.debug(message)
            del message
        else:
            # send the global model to selected clients
            for idx in tqdm(sampled_client_indices, leave=False):
                self.clients[idx].update_model(self.model.state_dict(), self._generator.state_dict())
            message = f"[Round: {str(self._round).zfill(3)}] ...successfully transmitted models to {str(len(sampled_client_indices))} selected clients!"
            logging.debug(message)
            del message

    def aggregate(self, sampled_client_indices, coefficients):
        """Average the updated and transmitted parameters from each selected client."""
        message = f"[Round: {str(self._round).zfill(3)}] Aggregate updated weights of {len(sampled_client_indices)} clients...!"
        logging.debug(message)
        del message

        averaged_weights = OrderedDict()
        averaged_generator_weights = OrderedDict()
        for it, idx in tqdm(enumerate(sampled_client_indices), leave=False):
            local_weights = self.clients[idx].model.state_dict()
            local_generator_weights = self.clients[idx].generator.state_dict()
            for key in self.model.state_dict().keys():
                if it == 0:
                    averaged_weights[key] = coefficients[it] * local_weights[key]
                    averaged_generator_weights[key] = coefficients[it] * local_generator_weights[key]
                    
                else:
                    averaged_weights[key] += coefficients[it] * local_weights[key]
                    averaged_generator_weights[key] += coefficients[it] * local_generator_weights[key]
        self.model.load_state_dict(averaged_weights)
        self.generator.load_state_dict(averaged_generator_weights)
        message = f"[Round: {str(self._round).zfill(3)}] ...updated weights of {len(sampled_client_indices)} clients are successfully averaged!"
        logging.debug(message)
        del message
