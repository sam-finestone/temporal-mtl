import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data.dataset import Dataset
import torch.utils.data.sampler as sampler
import os
import random
import time
import mlflow
from tensorboardX import SummaryWriter
import mlflow.pytorch
from utils.metrics import ConfMatrix, depth_error, \
    AverageMeter, ProgressMeter, iouCalc, visim, vislbl, depth_error2
from loss.loss import model_fit, DiceLoss, DiceBCELoss
import numpy as np
import logging
import cv2
import matplotlib.pyplot as plt
from utils.metrics import compute_miou
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
from torch.autograd import Variable
from utils.metrics_seg import StreamSegMetrics
from PIL import Image
import matplotlib
import matplotlib.pyplot as plt

########################################## STATIC TRAINING/EVALUTATION ########################################

def static_single_task_trainer(epoch, criterion, train_loader, model, model_opt, scheduler, task, LOG_FILE):
    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    loss_running = AverageMeter('Loss', ':.4e')
    acc_running = AverageMeter('Accuracy', ':.3f')
    abs_error_running = AverageMeter('Absolute error', ':.3f')
    rel_error_running = AverageMeter('Relative error', ':.3f')
    miou_running = AverageMeter('Miou', ':.3f')
    metrics = StreamSegMetrics(19)
    if task == 'segmentation':
        progress = ProgressMeter(
            len(train_loader),
            [batch_time, data_time, loss_running, acc_running, miou_running],
            prefix="Train, epoch: [{}]".format(epoch))
    if task == 'depth':
        progress = ProgressMeter(
            len(train_loader),
            [batch_time, data_time, loss_running, abs_error_running, rel_error_running],
            prefix="Train, epoch: [{}]".format(epoch))
    if task == 'depth_segmentation':
        progress = ProgressMeter(
            len(train_loader),
            [batch_time, data_time, loss_running, abs_error_running, rel_error_running, acc_running, miou_running],
            prefix="Train, epoch: [{}]".format(epoch))

    model.train()
    # conf_mat = ConfMatrix(19)
    # initialise the loss the function
    # metric_calculator = SegmentationMetrics(average=True, ignore_background=True)
    end = time.time()
    for batch_idx, (inputs, labels, depth) in enumerate(train_loader):
        data_time.update(time.time() - end)
        # [8, 1, 256, 512]
        model_opt.zero_grad()
        inputs = inputs.to(device, dtype=torch.float32)
        # torch.Size([8, 1, 256, 512])
        gt_semantic_labels = labels.to(device, dtype=torch.long)
        # torch.Size([8, 3, 256, 512])
        gt_depth = depth.to(device, dtype=torch.float32)
        if task == 'segmentation':
            task_pred = model(inputs)
            loss = criterion(task_pred, gt_semantic_labels)
            loss.backward()
            model_opt.step()

            # Get metrics
            task_pred = task_pred.detach().max(dim=1)[1].cpu().numpy()
            gt_semantic_labels = gt_semantic_labels.cpu().numpy()
            # loss = criterion(task_pred, gt_semantic_labels.squeeze())
            metrics.update(gt_semantic_labels, task_pred)
            curr_mean_acc = metrics.get_results()['Mean Acc']
            curr_mean_iou = metrics.get_results()['Mean IoU']
            # store loss
            bs = inputs.size(0)
            # loss = loss.item()
            loss = loss.detach().cpu().numpy()
            loss_running.update(loss, bs)
            acc_running.update(curr_mean_acc, bs)
            miou_running.update(curr_mean_iou, bs)


        if task == 'depth':
            task_pred = model(inputs)
            # print(gt_depth[0])
            # print(task_pred[0])
            # task_pred = task_pred.squeeze()
            # gt_depth = gt_depth.squeeze()
            loss = criterion(task_pred, gt_depth)
            loss.backward()
            model_opt.step()

            # store loss
            bs = inputs.size(0)
            loss = loss.item()
            loss_running.update(loss, bs)

            # compute the depth metrics
            # abs_err, rel_err = depth_error(task_pred, gt_depth)
            abs_err, rel_err = depth_error2(task_pred, gt_depth)
            abs_error_running.update(abs_err)
            rel_error_running.update(rel_err)

        if task == 'depth_segmentation':
            depth_pred, seg_pred = model(inputs)
            # task_pred = model(inputs)
            # print(depth_pred.shape)
            # break

            depth_loss = criterion[0](depth_pred, gt_depth)
            seg_loss = criterion[1](seg_pred, gt_semantic_labels)
            # Equal Weighted losses
            depth_weight = 0.5
            seg_weight = 0.5
            total_loss = (depth_weight * depth_loss) + (seg_loss * seg_weight)
            total_loss.backward()
            model_opt.step()

            # Get seg metrics
            seg_pred = seg_pred.detach().max(dim=1)[1].cpu().numpy()
            gt_semantic_labels = gt_semantic_labels.cpu().numpy()
            # loss = criterion(task_pred, gt_semantic_labels.squeeze())
            metrics.update(gt_semantic_labels, seg_pred)
            curr_mean_acc = metrics.get_results()['Mean Acc']
            curr_mean_iou = metrics.get_results()['Mean IoU']

            # store total loss
            bs = inputs.size(0)
            total_loss = total_loss.item()
            loss_running.update(total_loss, bs)
            acc_running.update(curr_mean_acc, bs)
            miou_running.update(curr_mean_iou, bs)

            # get depth metric
            # abs_err, rel_err = depth_error(depth_pred, gt_depth)
            abs_err, rel_err = depth_error2(depth_pred, gt_depth)
            abs_error_running.update(abs_err)
            rel_error_running.update(rel_err)

        # output batch info
        progress.display(batch_idx)
        if batch_idx % 25 == 0:
            if task == 'segmentation':
                with open(os.path.join(LOG_FILE, 'log_train_batch.txt'), 'a') as batch_log:
                    batch_log.write('{}, {:.5f}, {:.5f}, {:.5f}\n'.format(epoch, batch_idx, loss,
                                                                          metrics.get_results()['Mean Acc']))
            if task == 'depth':
                with open(os.path.join(LOG_FILE, 'log_train_batch.txt'), 'a') as batch_log:
                    batch_log.write('{}, {}, {:.5f}, {:.5f}, {:.5f}\n'.format(epoch, batch_idx, loss,
                                                                              abs_err, rel_err))

        # Measure time
        batch_time.update(time.time() - end)
        end = time.time()

    # reduce the learning rate
    scheduler.step(loss_running.avg)
    if task == 'depth':
        return loss_running.avg, abs_error_running.avg, rel_error_running.avg
    results = metrics.get_results()
    mean_acc = results['Mean Acc']
    mean_miou = results['Mean IoU']
    if task == 'segmentation':
        return loss_running.avg, mean_acc, mean_miou
    return loss_running.avg, abs_error_running.avg, rel_error_running.avg, mean_acc, mean_miou


def static_test_single_task(epoch, criterion, test_loader, single_task_model, task, classLabels, validClasses,
                            folder, void=0, maskColors=None, save_val_imgs=None):
    # evaluating test data
    # SAMPLES_PATH
    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    loss_running = AverageMeter('Loss', ':.4e')
    acc_running = AverageMeter('Accuracy', ':.4e')
    abs_error_running = AverageMeter('Absolute error', ':.3f')
    rel_error_running = AverageMeter('Relative error', ':.3f')
    miou_running = AverageMeter('Miou', ':.3f')
    metrics = StreamSegMetrics(19)

    if task == 'segmentation':
        # iou = iouCalc(classLabels, validClasses, voidClass=void)
        progress = ProgressMeter(
            len(test_loader),
            [batch_time, data_time, loss_running, acc_running, miou_running],
            prefix="Train, epoch: [{}]".format(epoch))

    if task == 'depth':
        progress = ProgressMeter(
            len(test_loader),
            [batch_time, data_time, loss_running, abs_error_running, rel_error_running],
            prefix="Train, epoch: [{}]".format(epoch))

    if task == 'depth_segmentation':
        # iou = iouCalc(classLabels, validClasses, voidClass=void)
        progress = ProgressMeter(
            len(test_loader),
            [batch_time, data_time, loss_running, abs_error_running, rel_error_running, acc_running, miou_running],
            prefix="Train, epoch: [{}]".format(epoch))

    single_task_model.eval()
    # conf_mat = ConfMatrix(20)
    # metric_calculator = SegmentationMetrics(average=True, ignore_background=True)
    end = time.time()
    img_id = 0
    with torch.no_grad():
        for batch_idx, (inputs, labels, depth, filepath) in enumerate(test_loader):
            inputs = inputs.to(device, dtype=torch.float32)
            gt_semantic_labels = labels.to(device, dtype=torch.long)
            gt_depth = depth.to(device)
            task_pred = single_task_model(inputs)
            if task == 'segmentation':
                loss = criterion(task_pred, gt_semantic_labels)

                task_pred = task_pred.detach().max(dim=1)[1].cpu().numpy()
                gt_semantic_labels = gt_semantic_labels.cpu().numpy()
                metrics.update(gt_semantic_labels, task_pred)

                bs = inputs.size(0)
                loss = loss.item()
                loss_running.update(loss, bs)

                # miou_score = compute_miou(task_pred, gt_semantic_labels).item()
                # miou_running.update(miou_score)
                # task_pred = torch.argmax(task_pred, dim=1)
                # corrects = torch.sum(task_pred == gt_semantic_labels.data)
                # void = 0
                # nvoid = int((gt_semantic_labels == void).sum())
                # res = 256 * 128
                # acc = corrects.cpu().double() / (bs * res - nvoid)
                # acc_running.update(acc, bs)
                # Calculate IoU scores of current batch
                # iou.evaluateBatch(task_pred, gt_semantic_labels)

                # Save visualizations of first batch
                if batch_idx == 0 and save_val_imgs is not None:
                    img_id = save_val_results_seg(inputs, gt_semantic_labels, task_pred, img_id, test_loader, folder)
                    # imgs = inputs.data.cpu().numpy()
                    # for i in range(inputs.size(0)):
                    #     filename = filepath[i]
                    #     save_visualization_segmentation(i, epoch, imgs, task_pred, gt_semantic_labels,
                    #                                     maskColors, filename, folder)


            if task == 'depth':
                loss = criterion(task_pred, gt_depth)
                bs = inputs.size(0)  # current batch size
                loss = loss.item()
                loss_running.update(loss, bs)
                abs_err, rel_err = depth_error2(task_pred, gt_depth)
                # abs_err, rel_err = depth_error(task_pred, gt_depth)
                abs_error_running.update(abs_err)
                rel_error_running.update(rel_err)

                # Save visualizations of first batch
                if batch_idx == 0 and save_val_imgs is not None:
                    imgs = inputs.data.cpu().numpy()
                    gt_depth_ = gt_depth.data.cpu().numpy()
                    pred_depth_ = task_pred.data.cpu().numpy()
                    for i in range(inputs.size(0)):
                        filename = filepath[i]
                        save_visualization_depth(i, epoch, imgs, pred_depth_, gt_depth_, filename, test_loader, folder)

            if task == 'depth_segmentation':
                depth_pred, seg_pred = single_task_model(inputs)
                seg_loss = criterion[1](seg_pred, gt_semantic_labels)
                depth_loss = criterion[0](depth_pred, gt_depth)

                # Equal Weighted losses
                depth_weight = 0.5
                seg_weight = 0.5
                total_loss = (depth_weight * depth_loss) + (seg_loss * seg_weight)

                seg_pred = seg_pred.detach().max(dim=1)[1].cpu().numpy()
                gt_semantic_labels = gt_semantic_labels.cpu().numpy()
                metrics.update(gt_semantic_labels, seg_pred)

                bs = inputs.size(0)  # current batch size
                loss = total_loss.item()
                loss_running.update(loss, bs)

                abs_err, rel_err = depth_error2(depth_pred, gt_depth)
                abs_error_running.update(abs_err)
                rel_error_running.update(rel_err)

                # Save visualizations of first batch
                if batch_idx == 0 and save_val_imgs is not None:
                    img_id = save_val_results_seg(inputs, gt_semantic_labels, seg_pred, img_id, test_loader, folder)
                    imgs = inputs.data.cpu().numpy()
                    gt_depth_ = gt_depth.data.cpu().numpy()
                    pred_depth_ = depth_pred.data.cpu().numpy()
                    for i in range(inputs.size(0)):
                        filename = filepath[i]
                        save_visualization_depth(i, epoch, imgs, pred_depth_, gt_depth_, filename, test_loader, folder)


        batch_time.update(time.time() - end)
        end = time.time()

        # print progress info
        progress.display(epoch)

    if task == 'depth':
        print('Abs. Error      : {:5.3f}'.format(abs_error_running.avg))
        print('Rel Error      : {:5.3f}'.format(rel_error_running.avg))
        print('---------------------')
        return rel_error_running.avg, abs_error_running.avg, loss_running.avg

    if task == 'segmentation':
        # miou = iou.outputScores()
        result = metrics.get_results()
        print('Accuracy      : {:5.3f}'.format(result['Mean Acc']))
        print('Miou      : {:5.3f}'.format(result['Mean IoU']))
        print('---------------------')
        return result['Mean Acc'], loss_running.avg, result['Mean IoU']
    # output for multi task learning
    print('Abs. Error      : {:5.3f}'.format(abs_error_running.avg))
    print('Rel Error      : {:5.3f}'.format(rel_error_running.avg))
    print('Accuracy      : {:5.3f}'.format(acc_running.avg))
    print('Miou      : {:5.3f}'.format(miou_running.avg))
    print('---------------------')
    return loss_running.avg, abs_error_running.avg, rel_error_running.avg, acc_running.avg, miou_running.avg


def lin_interp(shape, xyd):
    # taken from https://github.com/hunse/kitti
    from scipy.interpolate import LinearNDInterpolator
    m, n = shape
    ij, d = xyd[:, 1::-1], xyd[:, 2]
    f = LinearNDInterpolator(ij, d, fill_value=0)
    J, I = np.meshgrid(np.arange(n), np.arange(m))
    IJ = np.vstack([I.flatten(), J.flatten()]).T
    disparity = f(IJ).reshape(shape)
    return disparity

def save_val_results_seg(images, gt_semantic_labels, task_pred, img_id, loader, img_save_path):
    denorm = Denormalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    for i in range(len(images)):
        image = images[i].detach().cpu().numpy()
        target = gt_semantic_labels[i]
        pred = task_pred[i]

        image = (denorm(image) * 255).transpose(1, 2, 0).astype(np.uint8)
        target = loader.dataset.decode_target(target).astype(np.uint8)
        pred = loader.dataset.decode_target(pred).astype(np.uint8)

        Image.fromarray(image).save(os.path.join(img_save_path, '%d_image_seg.png' % img_id))
        Image.fromarray(target).save(os.path.join(img_save_path, '%d_target_seg.png' % img_id))
        Image.fromarray(pred).save(os.path.join(img_save_path, '%d_pred_seg.png' % img_id))

        fig = plt.figure()
        plt.imshow(image)
        plt.axis('off')
        plt.imshow(pred, alpha=0.7)
        ax = plt.gca()
        ax.xaxis.set_major_locator(matplotlib.ticker.NullLocator())
        ax.yaxis.set_major_locator(matplotlib.ticker.NullLocator())
        plt.savefig(os.path.join(img_save_path, '%d_overlay.png' % img_id), bbox_inches='tight', pad_inches=0)
        plt.close()
        img_id += 1
    return img_id

def save_visualization_segmentation(index, epoch, imgs, seg_pred, gt_semantic_labels, maskColors, filename, folder):
    denorm = Denormalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    #img_input = np.transpose(imgs[index], (1, 2, 0))
    img_input = (denorm(imgs[index]) * 255).transpose(1, 2, 0).astype(np.uint8)
    gt_id_format = gt_semantic_labels[index, :, :].squeeze()
    gt_color_format = vislbl(gt_id_format, maskColors)
    cv2.imwrite(folder + '/images/{}_epoch_{}_img_seg.png'.format(filename, epoch), img_input)
    cv2.imwrite(folder + '/images/{}_epoch_{}_gt_seg.png'.format(filename, epoch), gt_color_format)
    # Save predictions
    task_pred_id_format = seg_pred[index, :, :]
    pred_color_format = vislbl(task_pred_id_format, maskColors)
    cv2.imwrite(folder + '/images/{}_epoch_{}_pred_seg.png'.format(filename, epoch), pred_color_format)
    mlflow.log_artifact(folder + '/images/{}_epoch_{}_img_seg.png'.format(filename, epoch))
    mlflow.log_artifact(folder + '/images/{}_epoch_{}_gt_seg.png'.format(filename, epoch))
    mlflow.log_artifact(folder + '/images/{}_epoch_{}_pred_seg.png'.format(filename, epoch))

def save_visualization_depth(index, epoch, imgs, pred_depth_, gt_depth_, filename, loader, folder):
    denorm = Denormalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    # img_input = np.transpose(imgs[index], (1, 2, 0))
    img_input = (denorm(imgs[index]) * 255).transpose(1, 2, 0).astype(np.uint8)
    # pred_target = pred_depth_[index][0] / 255
    # pred_target = loader.dataset.unmap_disparity(pred_depth_[index][0])
    pred_target = pred_depth_[index][0].squeeze()  # [128, 256]
    # pred_target = cv2.normalize(pred_target, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)
    # pred_target = pred_target.astype(np.uint8)
    # pred_target = loader.dataset.map_to_rgb(pred_depth_[index][0])
    img_gt = gt_depth_[index][0].squeeze()
    # img_gt = cv2.normalize(img_gt, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_32F)
    # img_gt = img_gt.astype(np.uint8)
    # img_gt = loader.dataset.map_to_rgb(gt_depth_[index][0])
    # img_gt = gt_depth_[index][0]  # [128, 256]
    # img_gt = loader.dataset.map_to_rgb(gt_depth_[index][0])
    # img_gt = loader.dataset.unmap_disparity(gt_depth_[index][0])
    # print('predcited image max value: ' + str(pred_target.max()))
    # print('predcited image min value: ' + str(pred_target.min()))
    # print('actual image max value: ' + str(img_gt.max()))
    # print('actual image min value: ' + str(img_gt.min()))
    plt.imshow(img_gt)
    plt.savefig(folder + '/images/{}_epoch_{}_gt_depth.png'.format(filename, epoch))
    plt.close()
    plt.imshow(pred_target)
    plt.savefig(folder + '/images/{}_epoch_{}_pred_depth.png'.format(filename, epoch))
    plt.close()
    plt.imshow(img_input)
    plt.savefig(folder + '/images/{}_epoch_{}_img_depth.png'.format(filename, epoch))
    plt.close()
    # pred_target = cv2.applyColorMap(pred_target, cv2.COLORMAP_VIRIDIS)
    # img_gt = cv2.applyColorMap(img_gt, cv2.COLORMAP_VIRIDIS)
    # cv2.imwrite(folder + '/images/{}_epoch_{}_img_depth.png'.format(filename, epoch), img_input)
    # cv2.imwrite(folder + '/images/{}_epoch_{}_pred_depth.png'.format(filename, epoch), pred_target)
    # cv2.imwrite(folder + '/images/{}_epoch_{}_gt_depth.png'.format(filename, epoch), img_gt)
    # mlflow.log_artifact(folder + '/images/{}_epoch_{}_img_depth.png'.format(filename, epoch))
    # mlflow.log_artifact(folder + '/images/{}_epoch_{}_gt_depth.png'.format(filename, epoch))
    # mlflow.log_artifact(folder + '/images/{}_epoch_{}_pred_depth.png'.format(filename, epoch))
    # fig, (axs1, axs2, axs3) = plt.subplots(3, sharex=False, sharey=False)
    # plt.figure(figsize=(10, 10))
    # axs1.imshow(img_input)
    # axs2.imshow(pred_target)
    # plt.title("Disparity prediction", fontsize=22)
    # axs2.axis('off')
    # axs3.imshow(img_gt)
    # plt.title("Disparity actual", fontsize=22)
    # fig.savefig(folder + '/images/{}.png'.format(filename + '_image_' + str(index)))
    # mlflow.log_artifact(folder + '/images/{}.png'.format(filename + '_image_' + str(index)))
    # plt.close(fig)

def save_ckpt(path, model, optimizer, scheduler, metrics, best_score, epoch):
    """ save current model
    """
    torch.save({
        "epoch": epoch,
        "model_state": model.module.state_dict(),
        "optimizer_state": optimizer.state_dict(),
        "scheduler_state": scheduler.state_dict(),
        "metrics": metrics,
        "best_score": best_score,
    }, path)
    print("Model saved as %s" % path)

class Denormalize(object):
    def __init__(self, mean, std):
        mean = np.array(mean)
        std = np.array(std)
        self._mean = -mean/std
        self._std = 1/std

    def __call__(self, tensor):
        if isinstance(tensor, np.ndarray):
            return (tensor - self._mean.reshape(-1,1,1)) / self._std.reshape(-1,1,1)
        return normalize(tensor, self._mean, self._std)
# stereo = cv2.StereoBM_create(numDisparities=16, blockSize=15)
# disparity = stereo.compute(img_input, pred_target)
# plt.imshow(disparity)
# plt.show()
# plt.savefig(folder + '/images/{}.png'.format(filename + '_image_' + str(i)))
# plt.close()
# Only save inputs and labels once
# Saving colormapped depth image
# vmax = np.percentile(pred_target, 95)
# fig, (axs1, axs2, axs3) = plt.subplots(3, sharex=False, sharey=False)
# plt.figure(figsize=(10, 10))
# y, x = np.where(img_gt > 0)
# d = img_gt[img_gt != 0]
# xyd = np.stack((x, y, d)).T
# gt = lin_interp(img_gt.shape, xyd)
# axs1.imshow(img_input)
# plt.title("Input", fontsize=22)
# plt.axis('off')
# plt.subplot(212)


# def decode_pred(input, validClasses):
#     # Put all void classes to zero
#     for i in range(input.size(0)):
#         for _predc in range(len(validClasses)):
#             input[i, input == _predc, input == _predc] = validClasses[_predc]
#     return input


