import numpy as np
import cv2
from keras import backend as K
from skimage import measure
import matplotlib.pyplot as plt
from operator import sub
import tensorflow as tf


class SuperRoI:  # or rename it to ClassRoI
    def __init__(self, image =None):
        self.image = image
        self.roi = 1
        self.fullroi = None
        self.i = None
        self.j = None

    def setRoIij(self):
        print("Shape of RoI: ", self.roi.shape)
        self.i = np.where(self.roi == 1)[0]
        self.j = np.where(self.roi == 1)[1]
        print("Lengths of i and j index lists:", len(self.i), len(self.j))
        #print(self.i)
        #print(self.j)

    def meshgrid(self):
        # mesh for contour
        ylist = np.linspace(0, self.image.shape[0], self.image.shape[0])
        xlist = np.linspace(0, self.image.shape[1], self.image.shape[1])
        return np.meshgrid(xlist, ylist) #returns X,Y


class ClassRoI(SuperRoI):
    def __init__(self, model, image, cls, true_label, tp=False, fp=False):
        preds = model.predict(np.expand_dims(image, 0))[0]
        preds = tf.math.sigmoid(preds)
        preds = tf.cast(preds >= 0.5, 'float32')
        self.image = image
        self.true_label = tf.squeeze(true_label)



        mask = preds if cls == 1 else 1 - preds
        self.roi = np.round(mask).reshape(image.shape[-3], image.shape[-2])
        self.tp_mask = tf.cast(self.roi * self.true_label, 'float32')
        self.fp_mask = self.roi * (1 - self.true_label.numpy())

        TP = np.sum(self.tp_mask)
        FP = np.sum(self.fp_mask)
        FN = np.sum(true_label) - TP  # FN 是真实标签中为正但被预测为负的

        # 计算 precision 和 recall
        precision = TP / (TP + FP) if (TP + FP) > 0 else 0
        recall = TP / (TP + FN) if (TP + FN) > 0 else 0

        print(f"Precision: {precision:.4f}")
        print(f"Recall: {recall:.4f}")
        if tp:
            self.roi = self.tp_mask
        elif fp:
            self.roi = self.fp_mask
        self.fullroi = self.roi
        self.setRoIij()

    def connectedComponents(self):
        all_labels = measure.label(self.fullroi, background=0)
        (values, counts) = np.unique(all_labels * (all_labels != 0), return_counts=True)
        print("connectedComponents values, counts: ", values, counts)
        return all_labels, values, counts

    def largestComponent(self):
        all_labels, values, counts = self.connectedComponents()
        # find the largest component
        ind = np.argmax(counts[values != 0]) + 1  # +1 because indexing starts from 0 for the background
        print("argmax: ", ind)
        # define RoI
        self.roi = (all_labels == ind).astype(int)
        self.setRoIij()

    def smallestComponent(self):
        all_labels, values, counts = self.connectedComponents()
        ind = np.argmin(counts[values != 0]) + 1
        print("argmin: ", ind)  #
        self.roi = (all_labels == ind).astype(int)
        self.setRoIij()


class PixelRoI(SuperRoI):
    def __init__(self, i, j, image):
        self.image = image
        self.roi = np.zeros((image.shape[-3], image.shape[-2]))
        self.roi[i, j] = 1
        self.i = i
        self.j = j


class BiasRoI(SuperRoI):
    def __init__(self, next_batch, image_id):
        self.id = image_id
        self.image = next_batch[0][image_id][..., 0]
        self.gt_mask = next_batch[1][image_id]  # shape: (64,64,11)
        # self.tile_dict = next_batch[2][image_id]#[...,0]
        self.biased_tile = next_batch[2][image_id]['biased_tile'][..., 0]
        self.is_biased = next_batch[2][image_id]['is_biased']  # True or False
        self.background = next_batch[2][image_id]['background'][..., 0]
        self.digit_with_infill = next_batch[2][image_id]['digit_with_infill'][..., 0]

        self.biased_mask = self.biased_tile * self.background

    def biasedMask(self):
        plt.title('Biased mask for image ' + str(self.id))
        plt.imshow(self.biased_mask)
        plt.colorbar()
        return self.biased_mask
        # save?

    def unbiasedMask(self):

        c = sub(self.background, self.biased_tile)
        print(c.shape)
        c = np.ones(c.shape) * [c > 0]  # np.max(c,0)
        B = c[0]
        plt.title('Unbiased mask for image ' + str(self.id))
        plt.imshow(B)
        plt.colorbar()
        return B

    def biasedTextureContour(self):
        # TODO: draw the contour around the image border where the biased mask is

        # mesh for contour
        X, Y = self.meshgrid()
        plt.figure()
        plt.imshow(self.image, cmap='gray')
        plt.contour(X, Y, self.biased_mask)  # colors=c)

        plt.title('Contour for the biased mask')

        plt.xlabel('x')
        plt.ylabel('y')
        plt.show()


class SegGradCAM:
    """Seg-Grad-CAM method for explanations of predicted segmentation masks.
    Seg-Grad-CAM is applied locally to produce heatmaps showing the relevance of a set of pixels
    or an individual pixel for semantic segmentation.
    """

    def __init__(self, input_model, image, cls=-1, prop_to_layer='activation_9', prop_from_layer='last',
                 roi=SuperRoI(),  # 1, #default: explain all the pixels that belong to cls
                 normalize=True, abs_w=False, posit_w=False):

        self.input_model = input_model
        self.image = image
        #if cls == None:
        # TODO: add option cls=-1 (predicted class) and cls=None (gt class)
        # TODO print model's confidence (probability) in prediction
        self.cls = cls  # class
        # prop_from_layer is the layer with logits prior to the last activation function
        if prop_from_layer == 'last':
            self.prop_from_layer = self.input_model.layers[-1].name
        else:
            self.prop_from_layer = prop_from_layer
        self.prop_to_layer = prop_to_layer  # an intermediate layer, typically of the bottleneck layers

        self.roi = roi  # M, a set of pixel indices of interest in the output mask.
        self.normalize = normalize  # [True, False] normalize the saliency map L_c
        self.abs_w = abs_w  # if True, absolute function is applied to alpha_c
        self.posit_w = posit_w  # if True, ReLU is applied to alpha_c

        self.alpha_c = None  # alpha_c, weights for importance of feature maps
        self.A = None  # A, feature maps from the intermediate prop_to_layer
        self.grads_val = None  # gradients of the logits y with respect to all pixels of each feature map 𝐴^𝑘
        self.cam = None  # activation map L_c
        self.all_cam = None

        self.cam_max = None

    def featureMapsGradients(self):
        preprocessed_input = tf.expand_dims(self.image, 0)
        
        # 创建一个新模型，该模型将 input_model 的输入映射到 prop_to_layer 和 prop_from_layer 的输出
        intermediate_model = tf.keras.models.Model(inputs=self.input_model.input, 
                                                   outputs=[self.input_model.get_layer(self.prop_to_layer).output, 
                                                            self.input_model.get_layer(self.prop_from_layer).output])
        
        with tf.GradientTape(persistent=True) as tape:
            # 确保tape会跟踪任何可训练变量的访问
            tape.watch(intermediate_model.trainable_variables)
            conv_output, logits_output = intermediate_model(preprocessed_input)
            if logits_output.shape[-1] > 1:
                prob = logits_output[..., self.cls]
            else:
                prob = logits_output[..., 0] if self.cls == 1 else 1 - logits_output[..., 0]
            y_c = prob * self.roi.roi
            # y_c = logits_output[..., self.cls] * self.roi.roi
            loss = tf.reduce_sum(y_c)

        # 计算梯度
        grads = tape.gradient(loss, conv_output)
        
        self.A, self.grads_val = conv_output.numpy()[0, :], grads.numpy()[0, :, :, :]
        del tape  # 删除tape以释放资源
        return self.A, self.grads_val

    def gradientWeights(self):
        """Defines a matrix of alpha^k_c. Each alpha^k_c denotes importance (weights) of a feature map A^k for class c.
        If abs_w=True, absolute values of the matrix are processed and returned as weights.
        If posit_w=True, ReLU is applied to the matrix."""
        self.alpha_c = np.mean(self.grads_val, axis=(0, 1))
        if self.abs_w:
            self.alpha_c = abs(self.alpha_c)
        if self.posit_w:
            self.alpha_c = np.maximum(self.alpha_c, 0)

        return self.alpha_c

    def activationMap(self):
        """The last step to get the activation map. Should be called after outputGradients and gradientWeights."""
        # weighted sum of feature maps: sum of alpha^k_c * A^k
        cam = np.dot(np.maximum(self.A, 0), self.alpha_c)  # *abs(grads_val) or max(grads_val,0)
        # print(self.A)
        # all_cam = np.multiply(self.A, self.alpha_c[None, None, :])
        all_cam = np.multiply(self.A, np.maximum(self.alpha_c[None, None, :], 0))
        all_cam = np.maximum(all_cam, 0)
        # print("Shape of self.A:", self.A.shape)
        # print("Shape of self.alpha_c:", self.alpha_c.shape)
        # print("Shape of cam:", cam.shape)
        # print("Shape of all_cam:", all_cam.shape)


        img_dim = self.image.shape[:2]
        cam = cv2.resize(cam, img_dim[::-1], cv2.INTER_LINEAR)
        # apply ReLU to te sum
        # cam = np.maximum(cam, 0)
        # normalize non-negative weighted sum
        self.cam_max = cam.max()
        if self.cam_max != 0 and self.normalize:
            cam = cam / self.cam_max
        self.cam = cam
        self.all_cam = all_cam

        return self.cam, self.all_cam

    def SGC(self):
        """Get the activation map"""
        _, _ = self.featureMapsGradients()
        _ = self.gradientWeights()

        return self.activationMap()

    def __sub__(self, otherSGC):
        """Subtraction experiment"""
        pass

    def average(self, otherSGCs):
        """average several seg-grad-cams"""
        new_sgc = self.copy()
        cam = self.SGC()
        cams = [cam]
        if otherSGCs is list:
            for other in otherSGCs:
                cams.append(other.SGC())
        else:
            cams.append(otherSGCs)

        aver = None
        for cc in cams:
            aver += cc
            print("aver shape: ", aver.shape)

        new_sgc.cam = aver/len(cams)
        return new_sgc

    def sortbyMax(self):
        """sort a list of seg-grad-cams by their maximum in activation map before normalization
        for f in sorted(listofSGCs, key = lambda x: x.sortbyMax()):
        print(f.image, f.cls, f.prop_to_layer, f.roi, f.cam_max)
        """
        return self.cam_max
