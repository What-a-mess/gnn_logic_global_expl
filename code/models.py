import torch
import torch.nn.functional as F
from torch_geometric.nn import SAGEConv, GCNConv, global_mean_pool, global_add_pool, global_max_pool, GATConv, GINConv, GATv2Conv
from torch_scatter import scatter
import torch_explain as te
from torch_explain.logic.nn import entropy
from torch_explain.logic.metrics import test_explanation, complexity, test_explanations
from sklearn.metrics import accuracy_score
from sklearn.decomposition import PCA
from scipy.stats import hmean
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import wandb
import time
import pickle
import utils




class GlobalExplainer(torch.nn.Module):
    def __init__(self, len_model, le_model, dataloader, val_dataloader, device, hyper_params, classes_names, dataset_name):
        super().__init__()        
        
        self.le_model = le_model
        self.len_model = len_model
        
        self.prototype_vectors = torch.nn.Parameter(
            torch.rand((hyper_params["num_prototypes"], 
                        hyper_params["dim_prototypes"])), requires_grad=True)       
        
        self.train_metrics , self.val_metrics , self.train_logic_metrics , self.val_logic_metrics = [] , [] , [] , []
        self.device = device
        self.hyper = hyper_params
        self.dataloader = dataloader
        self.val_dataloader = val_dataloader
        self.classes_names = classes_names
        self.dataset_name = dataset_name
        self.temp = hyper_params["ts"]
        self.assign_func = hyper_params["assign_func"]
        self.early_stopping = utils.EarlyStopping(min_delta=0, patience=100)
        
        self.optimizer = torch.optim.Adam(le_model.parameters(), lr=self.hyper["le_emb_lr"])
        self.optimizer.add_param_group({'params': len_model.parameters(), 'lr': self.hyper["len_lr"]})
        self.optimizer.add_param_group({'params': self.prototype_vectors, 'lr': self.hyper["proto_lr"]})
        
        if hyper_params["focal_loss"]:
            self.loss_len = utils.focal_loss
        else:
            self.loss_len = utils.BCEWithLogitsLoss
        
        
    def get_concept_vector(self, loader, return_raw=False):
        le_embeddings = torch.tensor([], device=self.device)
        new_belonging = torch.tensor([], device=self.device, dtype=torch.long)
        y = torch.tensor([], device=self.device)
        le_classes = torch.tensor([], device=self.device)
        le_idxs = torch.tensor([], device=self.device)
        for data in loader:
            data = data.to(self.device)
            le_idxs = torch.concat([le_idxs, data.le_id], dim=0)
            embs = self.le_model(data.x, data.edge_index, data.batch)
            le_embeddings = torch.concat([le_embeddings, embs], dim=0)
            new_belonging = torch.concat([new_belonging, data.graph_id], dim=0)
            le_classes = torch.concat([le_classes, data.y], dim=0)
            y = torch.concat([y, data.task_y], dim=0)
            
        y = scatter(y, new_belonging, dim=0, reduce="max")
        y = torch.nn.functional.one_hot(y.long()).float().to(self.device)
             
        le_assignments = utils.prototype_assignement(self.hyper["assign_func"], le_embeddings, self.prototype_vectors, temp=1)        
        concept_vector = scatter(le_assignments, new_belonging, dim=0, reduce="max")  #"sum"
        #concept_vector = torch.clip(concept_vector, min=0, max=1)
        if return_raw:
            return concept_vector , le_embeddings ,  le_assignments , y , le_classes.cpu() , le_idxs , new_belonging
        else:
            return concept_vector , le_embeddings

    
    def train_epoch(self, loader, epoch, log_wandb=False, train=True):   
        if train:
            self.le_model.train()
            self.len_model.train()
        else:
            self.le_model.eval()
            self.len_model.eval()

        total_loss                     = torch.tensor(0., device=self.device)
        total_prototype_distance_loss  = torch.tensor(0., device=self.device)
        total_r1_loss                  = torch.tensor(0., device=self.device)
        total_r2_loss                  = torch.tensor(0., device=self.device)
        total_concept_entropy_loss     = torch.tensor(0., device=self.device)
        total_distribution_entropy_loss= torch.tensor(0., device=self.device)
        total_div_loss                 = torch.tensor(0., device=self.device)
        total_len_loss                 = torch.tensor(0., device=self.device)
        total_debug_loss               = torch.tensor(0., device=self.device)
        total_logic_loss               = torch.tensor(0., device=self.device)
        
        preds , trues = torch.tensor([], device=self.device) , torch.tensor([], device=self.device)
        le_classes = torch.tensor([], device=self.device)
        total_prototype_assignements = torch.tensor([], device=self.device)
        for data in loader:
            self.optimizer.zero_grad() 
            data = data.to(self.device)
            le_embeddings = self.le_model(data.x, data.edge_index, data.batch)
            
            new_belonging = torch.tensor(utils.normalize_belonging(data.graph_id), dtype=torch.long, device=self.device)
            y = scatter(data.task_y, new_belonging, dim=0, reduce="max")
            #y2 = scatter(data.task_y, new_belonging, dim=0, reduce="min")
            #assert torch.all(y == y2) #sanity check
            y_train_1h = torch.nn.functional.one_hot(y.long(), num_classes=2).float().to(self.device)
            
            prototype_assignements = utils.prototype_assignement(self.hyper["assign_func"], le_embeddings, self.prototype_vectors, temp=1)
            total_prototype_assignements = torch.cat([total_prototype_assignements, prototype_assignements], dim=0)
            le_classes = torch.concat([le_classes, data.y], dim=0)
            concept_vector = scatter(prototype_assignements, new_belonging, dim=0, reduce="max")  #"sum"
            #concept_vector = torch.clip(concept_vector, min=0, max=1)
            
            if self.hyper["debug_prototypes"]:
                # debug of Prototypes: do classification on prototypes
                loss , r1_loss , r2_loss , debug_loss = self.debug_prototypes(le_embeddings, prototype_assignements, data.y)
                total_loss += loss.detach()
                total_r1_loss += r1_loss.detach()
                total_r2_loss += r2_loss.detach()
                total_debug_loss += debug_loss.detach()
                preds = torch.cat([preds, prototype_assignements], dim=0)
                trues = torch.cat([trues, data.y], dim=0)
                continue
                
            # LEN
            y_pred = self.len_model(concept_vector).squeeze(-1)
            preds = torch.cat([preds, y_pred], dim=0)
            trues = torch.cat([trues, y_train_1h], dim=0)
            len_loss = 0.5 * self.loss_len(y_pred, y_train_1h, self.hyper["focal_gamma"], self.hyper["focal_alpha"])
            
            # Logic loss defined by Entropy Layer
            if self.hyper["coeff_logic_loss"] > 0:
                self.hyper["coeff_logic_loss"] * te.nn.functional.entropy_logic_loss(self.len_model)
            else:
                logic_loss = torch.tensor(0., device=self.device)
            
            
            # Prototype distance: push away the different prototypes by maximizing the distance to the nearest prototype
            if self.hyper["coeff_pdist"] > 0:
                prototype_distances = torch.clip(utils.pairwise_dist(self.prototype_vectors), max=0.5).fill_diagonal_(float("inf"))
                prototype_distances = prototype_distances.min(-1).values
                prototype_distance_loss = self.hyper["coeff_pdist"] * - torch.mean(prototype_distances)
            else:
                prototype_distance_loss = torch.tensor(0., device=self.device)
                
            # Div loss: from ProtGNN minimize the cosine similarity between prototypes with some margin
            if self.hyper["coeff_divloss"] > 0:
                proto_norm = F.normalize(self.prototype_vectors, p=2, dim=1)
                cos_distances = torch.mm(proto_norm, torch.t(proto_norm)) - torch.eye(proto_norm.shape[0]).to(self.device) - 0.2
                matrix2 = torch.zeros(cos_distances.shape).to(self.device)
                div_loss = self.hyper["coeff_divloss"] * torch.sum(torch.where(cos_distances > 0, cos_distances, matrix2))   
#                 scal_dot_product = torch.mm(self.prototype_vectors, torch.t(self.prototype_vectors)).fill_diagonal_(0.) / (self.prototype_vectors.shape[1]**0.5)
#                 matrix2 = torch.zeros(scal_dot_product.shape).to(self.device)
#                 div_loss = self.hyper["coeff_divloss"] * torch.sum(torch.where(scal_dot_product > 0, scal_dot_product, matrix2))   
            else:
                div_loss = torch.tensor(0., device=self.device)

            # R1 loss: push each prototype to be close to at least one example
            if self.hyper["coeff_r1"] > 0:
                sample_prototype_distance = torch.cdist(le_embeddings, self.prototype_vectors, p=2)**2 # num_sample x num_prototypes
                min_prototype_sample_distance = sample_prototype_distance.T.min(-1).values
                avg_prototype_sample_distance = torch.mean(min_prototype_sample_distance)
                r1_loss = self.hyper["coeff_r1"] * avg_prototype_sample_distance
            else:
                r1_loss = torch.tensor(0., device=self.device)

            # R2 loss: Push every example to be close to a sample
            if self.hyper["coeff_r2"] > 0:
                sample_prototype_distance = torch.cdist(le_embeddings, self.prototype_vectors, p=2)**2
                min_sample_prototype_distance = sample_prototype_distance.min(-1).values
                avg_sample_prototype_distance = torch.mean(min_sample_prototype_distance)
                r2_loss = self.hyper["coeff_r2"] * avg_sample_prototype_distance
            else:
                r2_loss = torch.tensor(0., device=self.device)

            # Entropy losses
            if self.hyper["coeff_ce"] > 0:
                concept_entropy_loss = self.hyper["coeff_ce"] * utils.entropy_loss(prototype_assignements)
            else:
                concept_entropy_loss = torch.tensor(0., device=self.device)
                
            if self.hyper["coeff_de"] > 0:
                distribution_entropy_loss = self.hyper["coeff_de"] * utils.entropy_loss(
                    torch.nn.functional.normalize(
                        torch.sum(prototype_assignements, dim=0),
                        p=2.0, dim=0).unsqueeze(0)
                )       
            else:
                distribution_entropy_loss = torch.tensor(0., device=self.device)


            loss = len_loss + logic_loss   + prototype_distance_loss + r1_loss + r2_loss + concept_entropy_loss + div_loss + distribution_entropy_loss
            total_loss                     += loss.detach()
            total_len_loss                 += len_loss.detach()
            total_prototype_distance_loss  += prototype_distance_loss.detach()
            total_r1_loss                  += r1_loss.detach()
            total_r2_loss                  += r2_loss.detach()
            total_concept_entropy_loss     += concept_entropy_loss.detach()
            total_div_loss                 += div_loss.detach()
            total_logic_loss               += logic_loss.detach()
            total_distribution_entropy_loss+= distribution_entropy_loss.detach()
            
            if train:
                loss.backward()
                self.optimizer.step()      
        
        if self.hyper["debug_prototypes"]:
            acc_per_class = accuracy_score(trues.cpu(), preds.argmax(-1).cpu())
            acc_overall = 0
        else:
            acc_per_class = accuracy_score(trues.argmax(-1).cpu(), preds.argmax(-1).cpu())
            acc_overall = sum(trues[:, :].eq(preds[:, :] > 0).sum(1) == 2) / len(preds)
        
        cluster_acc = utils.get_cluster_accuracy(
            total_prototype_assignements.argmax(1).detach().cpu().numpy(), 
            le_classes.cpu())
        metrics = {'loss': total_loss.item()/len(loader), 
                    "acc_per_class": acc_per_class, 
                    "acc_overall": acc_overall, 
                    "len_loss": total_len_loss.item()/len(loader),
                    "logic_loss": total_logic_loss.item()/len(loader),                    
                    "prototype_distance_loss": total_prototype_distance_loss.item()/len(loader),
                    "r1_loss:": total_r1_loss.item()/len(loader),
                    "r2_loss:": total_r2_loss.item()/len(loader),
                    "div_loss:": total_div_loss.item()/len(loader),
                    "debug_loss:": total_debug_loss.item()/len(loader),
                    "concept_entropy_loss:": total_concept_entropy_loss.item()/len(loader),
                    "distribution_entropy_loss:": total_distribution_entropy_loss.item()/len(loader),
                    "temperature": self.temp,
                    "cluster_acc_mean": np.mean(cluster_acc),
                    "cluster_acc_std": np.std(cluster_acc),
                    "concept_vector_entropy": utils.entropy_loss(prototype_assignements).detach().cpu(),
                    "prototype_assignements": wandb.Histogram(prototype_assignements.detach().cpu()),
                    "concept_vector": wandb.Histogram(concept_vector.detach().cpu()),
                   }
            
        if log_wandb:
            k = "train" if train else "val"
            self.log({k: metrics}) 
        else:
            if train:
                self.train_metrics.append(metrics)
            else:
                self.val_metrics.append(metrics)
        return metrics


    def debug_prototypes(self, le_embeddings, prototype_assignements, y):
        debug_loss = 1 * F.cross_entropy(prototype_assignements, y)

        sample_prototype_distance = torch.cdist(le_embeddings, self.prototype_vectors, p=2)**2 # num_sample x num_prototypes
        min_prototype_sample_distance = sample_prototype_distance.T.min(-1).values
        avg_prototype_sample_distance = torch.mean(min_prototype_sample_distance)
        r1_loss = self.hyper["coeff_r1"] * avg_prototype_sample_distance     

        min_sample_prototype_distance = sample_prototype_distance.min(-1).values
        avg_sample_prototype_distance = torch.mean(min_sample_prototype_distance)
        r2_loss = self.hyper["coeff_r2"] * avg_sample_prototype_distance

        loss = debug_loss +  r1_loss + r2_loss 
        loss.backward()
        self.optimizer.step()
        return loss, r1_loss , r2_loss , debug_loss
        
        
    def iterate(self, num_epochs, log_wandb=False, name_wandb="", save_metrics=True, plot=False):
        if log_wandb:
            self.run = wandb.init(
                    project='GlobalGraphXAI',
                    name=name_wandb,
                    entity='mcstewe',
                    reinit=True,
                    save_code=True,
                    config=self.hyper
            )
            wandb.watch(self.le_model)
            wandb.watch(self.len_model)        
        
        self.inspect_embedding(self.dataloader)        
        start_time = time.time()
        best_val_loss = np.inf
        for epoch in range(1, num_epochs):
            train_metrics = self.train_epoch(self.dataloader, epoch, log_wandb)
            val_metrics   = self.train_epoch(self.val_dataloader, epoch, log_wandb, train=False)
            
            if epoch % 20 == 0:
                self.inspect_embedding(self.dataloader, log_wandb, plot=plot)
                self.inspect_embedding(self.val_dataloader, log_wandb=False, plot=False, is_train_set=False)
                
            self.temp -= (self.hyper["ts"] - self.hyper["te"]) / num_epochs
            if log_wandb and self.hyper["log_models"]:
                torch.save(self.state_dict(), f"{wandb.run.dir}/epoch_{epoch}.pt")  
            if val_metrics["loss"] < best_val_loss and self.hyper["log_models"]:
                best_val_loss = val_metrics["loss"]
                torch.save(self.state_dict(), f"../trained_models/best_so_far_{self.dataset_name}_epoch_{epoch}.pt")
            print(f'{epoch:3d}: Loss: {train_metrics["loss"]:.5f}, LEN: {train_metrics["len_loss"]:2f}, AccxC: {train_metrics["acc_per_class"]:.2f}, AccO: {train_metrics["acc_overall"]:.2f}, V. Acc: {val_metrics["acc_overall"]:.2f}, V. Loss: {val_metrics["loss"]:.5f}, V. LEN {val_metrics["len_loss"]:.2f}')
                
            if self.early_stopping.on_epoch_end(epoch, val_metrics["loss"]):
                print(f"Early Stopping")
                print(f"Loading model at epoch {self.early_stopping.best_epoch}")
                if log_wandb and self.hyper["log_models"]:
                    self.load_state_dict(torch.load(f"{wandb.run.dir}/epoch_{self.early_stopping.best_epoch}.pt"))
                elif self.hyper["log_models"]:
                    self.load_state_dict(torch.load(f"../trained_models/best_so_far_{self.dataset_name}_epoch_{self.early_stopping.best_epoch}.pt"))
                else:
                    print("Model not loaded")
                break
        print(f"Best epoch: {self.early_stopping.best_epoch}")   
        print(f"Trained lasted for {round(time.time() - start_time)} seconds")
                
        if log_wandb:
            if self.hyper["log_models"]:
                wandb.save(f'{wandb.run.dir}/epoch_*.pt')
            self.run.finish()  
        
        if save_metrics and False:
            with open(f'../logs/ablation/num_proto/{self.dataset_name}/{self.hyper["num_prototypes"]}_train_metrics.pkl', 'wb') as handle:
                pickle.dump(self.train_metrics, handle)
            with open(f'../logs/ablation/num_proto/{self.dataset_name}/{self.hyper["num_prototypes"]}_val_metrics.pkl', 'wb') as handle:
                pickle.dump(self.val_metrics, handle)        
            with open(f'../logs/ablation/num_proto/{self.dataset_name}/{self.hyper["num_prototypes"]}_train_logic_metrics.pkl', 'wb') as handle:
                pickle.dump(self.train_logic_metrics, handle)        
            with open(f'../logs/ablation/num_proto/{self.dataset_name}/{self.hyper["num_prototypes"]}_val_logic_metrics.pkl', 'wb') as handle:
                pickle.dump(self.val_logic_metrics, handle)     
        return

    
    def inspect_embedding(self, loader, log_wandb=False, plot=True, is_train_set=False):
        self.le_model.eval()
        self.len_model.eval()
        
        x_train , emb , concepts_assignement , y_train_1h , le_classes , le_idxs , belonging = self.get_concept_vector(loader, return_raw=True)        
        y_pred = self.len_model(x_train).squeeze(-1)
        #loss = self.loss_len(y_pred, y_train_1h, self.hyper["focal_gamma"], self.hyper["focal_alpha"])
        #grads = torch.autograd.grad(outputs=loss, inputs=emb, grad_outputs=torch.ones_like(loss))[0].detach().cpu().numpy()
        #grads = np.zeros(emb.shape)
        
        with torch.no_grad():
            emb = emb.detach().cpu().numpy()
            concept_predictions = concepts_assignement.argmax(1).cpu().numpy()
        
            if plot:
                # plot embedding
                pca = PCA(n_components=2, random_state=42)
                emb2d = emb if self.prototype_vectors.shape[1] == 2 else pca.fit_transform(emb) #emb
                fig = plt.figure(figsize=(17,4))
                plt.subplot(1,2,1)
                plt.title("local explanations embeddings", size=23)
                print(np.unique(le_classes, return_counts=True))
                for c in np.unique(le_classes):
                    plt.scatter(emb2d[le_classes == c,0], emb2d[le_classes == c,1], label=self.classes_names[int(c)], alpha=0.7)
                proto_2d = self.prototype_vectors.cpu().numpy() if self.prototype_vectors.shape[1] == 2 else pca.transform(self.prototype_vectors.cpu().numpy())
                plt.scatter(proto_2d[:, 0], proto_2d[:,1], marker="x", s=60, c="black")        
                for i, txt in enumerate(range(proto_2d.shape[0])):
                    plt.annotate("p" + str(i), (proto_2d[i,0]+0.01, proto_2d[i,1]+0.01), size=27)
                plt.legend(bbox_to_anchor=(0.04,1), prop={'size': 17})
                plt.subplot(1,2,2)
                plt.title("prototype assignments", size=23)
                for c in range(self.prototype_vectors.shape[0]):
                    plt.scatter(emb2d[concept_predictions == c,0], emb2d[concept_predictions == c,1], label="p"+str(c))
                plt.legend(prop={'size': 17})
                # plt.subplot(1,3,3)
                # plt.title("predictions")
                # idx_belonging_correct = y_train_1h[:, :].eq(y_pred[:, :] > 0).sum(1) == 2 #y_pred.argmax(1) == y_train_1h.argmax(1)
                # idx_belonging_wrong   = y_train_1h[:, :].eq(y_pred[:, :] > 0).sum(1) != 2
                # colors = []
                # for idx in range(emb2d.shape[0]):
                #     if idx_belonging_correct[belonging[idx]]:
                #         colors.append("blue")
                #     elif idx_belonging_wrong[belonging[idx]]:
                #         colors.append("red")
                # plt.scatter(emb2d[:, 0], emb2d[:, 1], c=colors)
                # patches = [mpatches.Patch(color='blue', label='correct'), mpatches.Patch(color='red', label='wrong')]
                # plt.legend(handles=patches)
                # if log_wandb and self.hyper["log_images"]: 
                #     wandb.log({"plots": wandb.Image(plt)})
                # if self.prototype_vectors.shape[1] > 2: print(pca.explained_variance_ratio_)
                fig.supxlabel('principal comp. 1', size=20)
                #fig.supylabel('principal comp. 2', size=20)                
                #plt.savefig("embedding_mutagenicity.pdf")
                plt.show()          


            #log stats
            if isinstance(self.len_model[0], te.nn.logic.EntropyLinear) and plot:
                print("Alpha norms:")
                print(self.len_model[0].alpha_norm)

            
            x_train = x_train.detach()
            explanation0, explanation_raw, _ = entropy.explain_class(self.len_model.cpu(), x_train.cpu(), y_train_1h.cpu(), train_mask=torch.arange(x_train.shape[0]).long(), val_mask=torch.arange(x_train.shape[0]).long(), target_class=0, max_accuracy=True, topk_explanations=3000, try_all=False)
            accuracy1, preds = test_explanation(explanation0, x_train.cpu(), y_train_1h.cpu(), target_class=0, mask=torch.arange(x_train.shape[0]).long(), material=False)
            
            accs = utils.get_cluster_accuracy(concept_predictions, le_classes)
            if plot:
                print(f"Concept Purity: {np.mean(accs):2f} +- {np.std(accs):2f}")
                print("Concept distribution: ", np.unique(concept_predictions, return_counts=True))        
                print("Logic formulas:")
                print("For class 0:")
                print(accuracy1, utils.rewrite_formula_to_close(utils.assemble_raw_explanations(explanation_raw)))

            explanation1, explanation_raw, _ = entropy.explain_class(self.len_model.cpu(), x_train.cpu(), y_train_1h.cpu(), train_mask=torch.arange(x_train.shape[0]).long(), val_mask=torch.arange(x_train.shape[0]).long(), target_class=1, max_accuracy=True, topk_explanations=3000, try_all=False)
            accuracy2, preds = test_explanation(explanation1, x_train.cpu(), y_train_1h.cpu(), target_class=1, mask=torch.arange(x_train.shape[0]).long(), material=False)
            
            if plot:
                print("For class 1:")
                print(accuracy2, utils.rewrite_formula_to_close(utils.assemble_raw_explanations(explanation_raw)))

            accuracy, preds, unfiltered_pred = test_explanations([explanation0, explanation1], x_train.cpu(), y_train_1h.cpu(), model_predictions=y_pred, mask=torch.arange(x_train.shape[0]).long(), material=False, break_w_errors=False)
            if plot: print("Accuracy as classifier: ", round(accuracy, 4))
            if plot: print("LEN fidelity: ", sum(y_train_1h[:, :].eq(y_pred[:, :] > 0).sum(1) == 2) / len(y_pred))
            
            print()
            if log_wandb: self.log({"train": {'logic_acc': hmean([accuracy1, accuracy2]), "logic_acc_clf": accuracy}}) 
            else: 
                if is_train_set:
                    self.train_logic_metrics.append({'logic_acc': hmean([accuracy1, accuracy2]), "logic_acc_clf": accuracy, "concept_purity": np.mean(accs), "concept_purity_std": np.std(accs)})
                else:
                    self.val_logic_metrics.append({'logic_acc': hmean([accuracy1, accuracy2]), "logic_acc_clf": accuracy, "concept_purity": np.mean(accs), "concept_purity_std": np.std(accs)})
        self.len_model.to(self.device)
        #return local_explanations_0 , local_explanations_raw_0 , local_explanations_1 , local_explanations_raw_1
        
    def log(self, msg):
        wandb.log(msg)

    def eval(self):
        self.le_model.eval()
        self.len_model.eval()
        
# nn = torch.nn.Sequential(
#     torch.nn.Linear(num_features, num_gnn_hidden),
#     torch.nn.LeakyReLU(),
#     torch.nn.Dropout(dropout)
# )
# nn2 = torch.nn.Sequential(
#     torch.nn.Linear(num_gnn_hidden, num_gnn_hidden),
#     torch.nn.LeakyReLU(),
#     torch.nn.Dropout(dropout)
# )
# nn3 = torch.nn.Sequential(
#     torch.nn.Linear(num_gnn_hidden, num_gnn_hidden),
#     torch.nn.LeakyReLU(),
#     torch.nn.Dropout(dropout)
# )
# self.conv1 = GINConv(nn, train_eps=False) #, edge_dim=1
# self.conv2 = GINConv(nn2, train_eps=False)
# self.conv3 = GINConv(nn3, train_eps=False)   


class LEEmbedder(torch.nn.Module):
    def __init__(self, num_features, activation, num_gnn_hidden=20, dropout=0.1, num_hidden=10, num_layers=2, backbone="GIN"):
        super().__init__()

        if backbone == "GIN":
            nns = torch.nn.ModuleList([
                torch.nn.Sequential(
                    torch.nn.Linear(num_features if i == 0 else num_gnn_hidden, num_gnn_hidden),
                    torch.nn.LeakyReLU(),
                    torch.nn.Dropout(dropout)
                )
            for i in range(num_layers)
            ])
            self.convs = torch.nn.ModuleList([
                GINConv(nns[i], train_eps=False) for i in range(num_layers)
            ])
        elif backbone == "GAT":
            self.convs = torch.nn.ModuleList([
                GATv2Conv(num_features if i == 0 else num_gnn_hidden, int(num_gnn_hidden/4), heads=4) for i in range(num_layers)
            ])
        else:
            raise ValueError("Backbone not available") 

        self.proj = torch.nn.Linear(num_gnn_hidden * 3, num_hidden)
        self.num_layers = num_layers

        if activation == "sigmoid":
            self.actv = torch.nn.Sigmoid()
        elif activation == "tanh":
            self.actv = torch.nn.Tanh()
        elif activation == "leaky":
            self.actv = torch.nn.LeakyReLU()
        elif activation == "lin":
            self.actv = torch.nn.LeakyReLU(negative_slope=1)
        else:
            raise ValueError("Activation not available") 


    def forward(self, x, edge_index, batch):
        x = self.get_graph_emb(x, edge_index, batch)
        x = self.actv(self.proj(x))
        return x
    
    def get_graph_emb(self, x, edge_index, batch):
        x = self.get_emb(x, edge_index)

        x1 = global_mean_pool(x, batch)
        x2 = global_add_pool(x, batch)
        x3 = global_max_pool(x, batch)
        x = torch.cat([x1, x2, x3], dim=-1)
        return x

    def get_emb(self, x, edge_index):
        for i in range(self.num_layers):
            x = self.actv(self.convs[i](x.float(), edge_index))
        return x

    


def LEN(input_shape, temperature, remove_attention=False):
    layers = [
        te.nn.EntropyLinear(input_shape, 10, n_classes=2, temperature=temperature, remove_attention=remove_attention),
        torch.nn.LeakyReLU(),
        torch.nn.Linear(10, 5),
        torch.nn.LeakyReLU(),
        torch.nn.Linear(5, 1),
    ]
    return torch.nn.Sequential(*layers)

def MLP(input_shape):
    layers = [
        torch.nn.Linear(input_shape, 10),
        torch.nn.LeakyReLU(),
        torch.nn.Linear(10, 5),
        torch.nn.LeakyReLU(),
        torch.nn.Linear(5, 2),
    ]
    return torch.nn.Sequential(*layers)





