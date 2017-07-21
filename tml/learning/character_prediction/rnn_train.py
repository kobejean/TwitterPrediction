# encoding: UTF-8
# Copyright 2017 Google.com
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import tensorflow as tf
from tensorflow.contrib import layers
from tensorflow.contrib import rnn  # rnn stuff temporarily in contrib, moving back to code in TF 1.1
import os, sys, getopt, time, math
import numpy as np
from . import preprocessing as txt
tf.set_random_seed(0)

# model parameters
#
# Usage:
#   Training only:
#         Leave all the parameters as they are
#         Disable validation to run a bit faster (set validation=False below)
#         You can follow progress in Tensorboard: tensorboard --log-dir=log
#   Training and experimentation (default):
#         Keep validation enabled
#         You can now play with the parameters anf follow the effects in Tensorboard
#         A good choice of parameters ensures that the testing and validation curves stay close
#         To see the curves drift apart ("overfitting") try to use an insufficient amount of
#         training data (shakedir = "shakespeare/t*.txt" for example)
#
SEQSIZE = 60
BATCHSIZE = 100
ALPHASIZE = txt.ALPHASIZE
INTERNALSIZE = 1024#512
NLAYERS = 3
learning_rate = 0.001  # fixed learning rate
dropout_pkeep = 1.0    # no dropout


def train(text_files, log_path, checkpoints_path):
    print("TEXT FILES:", text_files)
    print("LOG PATH:", log_path)
    print("CHECKPOINTS PATH:", checkpoints_path)
    codelen, codetext, valitext, bookranges = txt.read_data_files(text_files, validation=True)
    vallen = len(valitext)
    testlen = codelen - vallen
    nb_batches = (testlen//BATCHSIZE)
    print("NUM BATCHES:", nb_batches)
    # display some stats on the data
    epoch_size = testlen // (BATCHSIZE * SEQSIZE)
    txt.print_data_stats(testlen, vallen, epoch_size)

    #
    # the model (see FAQ in README.md)
    #
    lr = tf.placeholder(tf.float32, name='lr')  # learning rate
    pkeep = tf.placeholder(tf.float32, name='pkeep')  # dropout parameter
    batchsize = tf.placeholder(tf.int32, name='batchsize')

    # inputs
    X = tf.placeholder(tf.uint8, [None, None], name='X')    # [ BATCHSIZE, SEQSIZE ]
    Xo = tf.one_hot(X, ALPHASIZE, 1.0, 0.0)                 # [ BATCHSIZE, SEQSIZE, ALPHASIZE ]
    # expected outputs = same sequence shifted by 1 since we are trying to predict the next character
    Y_ = tf.placeholder(tf.uint8, [None, None], name='Y_')  # [ BATCHSIZE, SEQSIZE ]
    Yo_ = tf.one_hot(Y_, ALPHASIZE, 1.0, 0.0)               # [ BATCHSIZE, SEQSIZE, ALPHASIZE ]
    # input state
    Hin = tf.placeholder(tf.float32, [None, INTERNALSIZE*NLAYERS], name='Hin')  # [ BATCHSIZE, INTERNALSIZE * NLAYERS]

    # using a NLAYERS=3 layers of GRU cells, unrolled SEQSIZE=30 times
    # dynamic_rnn infers SEQSIZE from the size of the inputs Xo
    gruCellsWithDropout = [rnn.DropoutWrapper(rnn.GRUCell(INTERNALSIZE), input_keep_prob=pkeep) for _ in range(NLAYERS)]
    multicell = rnn.MultiRNNCell(gruCellsWithDropout, state_is_tuple=False)
    multicell = rnn.DropoutWrapper(multicell, output_keep_prob=pkeep)
    Yr, H = tf.nn.dynamic_rnn(multicell, Xo, dtype=tf.float32, initial_state=Hin)
    # Yr: [ BATCHSIZE, SEQSIZE, INTERNALSIZE ]
    # H:  [ BATCHSIZE, INTERNALSIZE*NLAYERS ] # this is the last state in the sequence

    H = tf.identity(H, name='H')  # just to give it a name

    # Softmax layer implementation:
    # Flatten the first two dimension of the output [ BATCHSIZE, SEQSIZE, ALPHASIZE ] => [ BATCHSIZE x SEQSIZE, ALPHASIZE ]
    # then apply softmax readout layer. This way, the weights and biases are shared across unrolled time steps.
    # From the readout point of view, a value coming from a cell or a minibatch is the same thing

    Yflat = tf.reshape(Yr, [-1, INTERNALSIZE])    # [ BATCHSIZE x SEQSIZE, INTERNALSIZE ]
    Ylogits = layers.linear(Yflat, ALPHASIZE)     # [ BATCHSIZE x SEQSIZE, ALPHASIZE ]
    Yflat_ = tf.reshape(Yo_, [-1, ALPHASIZE])     # [ BATCHSIZE x SEQSIZE, ALPHASIZE ]
    loss = tf.nn.softmax_cross_entropy_with_logits(logits=Ylogits, labels=Yflat_)  # [ BATCHSIZE x SEQSIZE ]
    loss = tf.reshape(loss, [batchsize, -1])      # [ BATCHSIZE, SEQSIZE ]
    Yo = tf.nn.softmax(Ylogits, name='Yo')        # [ BATCHSIZE x SEQSIZE, ALPHASIZE ]
    Y = tf.argmax(Yo, 1)                          # [ BATCHSIZE x SEQSIZE ]
    Y = tf.reshape(Y, [batchsize, -1], name="Y")  # [ BATCHSIZE, SEQSIZE ]
    train_step = tf.train.AdamOptimizer(lr).minimize(loss)

    # stats for display
    seqloss = tf.reduce_mean(loss, 1)
    batchloss = tf.reduce_mean(seqloss)
    accuracy = tf.reduce_mean(tf.cast(tf.equal(Y_, tf.cast(Y, tf.uint8)), tf.float32))
    loss_summary = tf.summary.scalar("batch_loss", batchloss)
    acc_summary = tf.summary.scalar("batch_accuracy", accuracy)
    summaries = tf.summary.merge([loss_summary, acc_summary])

    # Init Tensorboard stuff. This will save Tensorboard information into a different
    # folder at each run named 'log/<timestamp>/'. Two sets of data are saved so that
    # you can compare training and validation curves visually in Tensorboard.
    timestamp = str(math.trunc(time.time()))
    summary_path = os.path.join(log_path, timestamp + "-training")
    summary_writer = tf.summary.FileWriter(summary_path)
    validation_path = os.path.join(log_path, timestamp + "-validation")
    validation_writer = tf.summary.FileWriter(validation_path)

    # Init for saving models. They will be saved into a directory named 'checkpoints'.
    # Only the last checkpoint is kept.
    if not os.path.exists(checkpoints_path):
        os.mkdir(checkpoints_path)
    saver = tf.train.Saver(max_to_keep=1)

    # for display: init the progress bar
    DISPLAY_FREQ = 50
    _50_BATCHES = DISPLAY_FREQ * BATCHSIZE * SEQSIZE
    progress = txt.Progress(DISPLAY_FREQ, size=111+2, msg="Training on next "+str(DISPLAY_FREQ)+" batches")

    # init
    istate = np.zeros([BATCHSIZE, INTERNALSIZE*NLAYERS])  # initial zero input state
    init = tf.global_variables_initializer()
    sess = tf.Session()
    sess.run(init)
    step = 0

    # training loop
    for x, y_, epoch in txt.rnn_minibatch_sequencer(codetext, BATCHSIZE, SEQSIZE, nb_epochs=1000000000, nb_batches=nb_batches):

        # train on one minibatch
        feed_dict = {X: x, Y_: y_, Hin: istate, lr: learning_rate, pkeep: dropout_pkeep, batchsize: BATCHSIZE}
        _, y, ostate, smm = sess.run([train_step, Y, H, summaries], feed_dict=feed_dict)

        # save training data for Tensorboard
        summary_writer.add_summary(smm, step)

        # display a visual validation of progress (every 50 batches)
        if step % _50_BATCHES == 0:
            feed_dict = {X: x, Y_: y_, Hin: istate, pkeep: 1.0, batchsize: BATCHSIZE}  # no dropout for validation
            y, l, bl, acc = sess.run([Y, seqloss, batchloss, accuracy], feed_dict=feed_dict)
            txt.print_learning_learned_comparison(x, y, l, bookranges, bl, acc, epoch_size, step, epoch)

        # run a validation step every 50 batches
        # The validation text should be a single sequence but that's too slow (1s per 1024 chars!),
        # so we cut it up and batch the pieces (slightly inaccurate)
        # tested: validating with 5K sequences instead of 1K is only slightly more accurate, but a lot slower.
        if step % _50_BATCHES == 0 and len(valitext) > 0:
            VALI_SEQSIZE = 1*1024  # Sequence length for validation. State will be wrong at the start of each sequence.
            bsize = len(valitext) // VALI_SEQSIZE
            txt.print_validation_header(testlen, bookranges)
            vali_x, vali_y, _ = next(txt.rnn_minibatch_sequencer(valitext, bsize, VALI_SEQSIZE, 1, bsize))  # all data in 1 batch
            vali_nullstate = np.zeros([bsize, INTERNALSIZE*NLAYERS])
            feed_dict = {X: vali_x, Y_: vali_y, Hin: vali_nullstate, pkeep: 1.0,  # no dropout for validation
                         batchsize: bsize}
            ls, acc, smm = sess.run([batchloss, accuracy, summaries], feed_dict=feed_dict)
            txt.print_validation_stats(ls, acc)
            # save validation data for Tensorboard
            validation_writer.add_summary(smm, step)

        # display a short text generated with the current weights and biases (every 150 batches)
        if step // 3 % _50_BATCHES == 0:
            txt.print_text_generation_header()
            ry = np.array([[txt.convert_from_alphabet(ord("\n"))]])
            rh = np.zeros([1, INTERNALSIZE * NLAYERS])
            for k in range(1000):
                ryo, rh = sess.run([Yo, H], feed_dict={X: ry, pkeep: 1.0, Hin: rh, batchsize: 1})
                rc = txt.sample_from_probabilities(ryo, topn=10 if epoch <= 1 else 2)
                print(chr(txt.convert_to_alphabet(rc)), end="")
                ry = np.array([[rc]])
            txt.print_text_generation_footer()

        # save a checkpoint (every 500 batches)
        if step // 10 % _50_BATCHES == 0:
            save_path = os.path.join(checkpoints_path, 'rnn_train_' + timestamp)
            saver.save(sess, save_path, global_step=step)

        # display progress bar
        progress.step(reset=step % _50_BATCHES == 0)

        # loop state around
        istate = ostate
        step += BATCHSIZE * SEQSIZE

    # all runs: SEQSIZE = 30, BATCHSIZE = 100, ALPHASIZE = 98, INTERNALSIZE = 512, NLAYERS = 3
    # run 1477669632 decaying learning rate 0.001-0.0001-1e7 dropout 0.5: not good
    # run 1477670023 lr=0.001 no dropout: very good

    # Tensorflow runs:
    # 1485434262
    #   trained on shakespeare/t*.txt only. Validation on 1K sequences
    #   validation loss goes up from step 5M
    # 1485436038
    #   trained on shakespeare/t*.txt only. Validation on 5K sequences
    #   On 5K sequences validation accuracy is slightly higher and loss slightly lower
    #   => sequence breaks do introduce inaccuracies but the effect is small
    # 1485437956
    #   Trained on shakespeare/*.txt only. Validation on 1K sequences
    #   On this much larger dataset, validation loss still decreasing after 6 epochs (step 35M)
    # 1485440785
    #   Dropout = 0.5 - Trained on shakespeare/*.txt only. Validation on 1K sequences
    #   Much worse than before. Not very surprising since overfitting was not apparent
    #   on the validation curves before so there is nothing for dropout to fix.

if __name__ == "__main__":
    # abs_path = os.path.abspath(os.path.dirname(__file__))
    # checkpoints_path = os.path.join(abs_path, "checkpoints")
    # log_path = os.path.join(abs_path, "log")
    # text_path = os.path.join(abs_path, "the")
    # text_files = os.path.join(text_path, "*.txt")
    text_files = None
    log_path = None
    checkpoints_path = None

    options = {}
    usage_str = "usage: python3 -m tml.learning.character_prediction.rnn_train <text_files> <log_path> <checkpoints_path>"
    try:
        text_files = sys.argv[1]
        log_path = sys.argv[2]
        checkpoints_path = sys.argv[3]
        # opts, args = getopt.getopt(sys.argv[2:],"hl:g:e:b:",["vp=","cp=","bs=","vs=","ws=","ss=","h1=","h2=","lr="])
    except (IndexError, getopt.GetoptError):
        print(usage_str)
        sys.exit(2)

    # eventually we should implement opts:

    # for opt, arg in opts:
    #     if opt == '-h':
    #         print(usage_str + "\n" +
    #         """
    #         options:
    #         -h                      show help menu
    #         -l <log_dir>            the path to the log directory
    #         -g <graph_path>         the checkpoint date the nn should restore from
    #         -e <epochs>             number of epochs the nn should run
    #         -b <batches>            number of batches the nn should run
    #
    #         -v <val_period>         how often to validate in batches per validation
    #         -c <cp_period>          how often to save checkpoints in number of batches per checkpoint
    #
    #         --bs <batch_size>       batch size
    #         --vs <val_size>         validation set size in number of batches
    #         --ws <vocab_size>       vocabulary/word size
    #         --ss <seq_size>         sub sequence size
    #         --h1 <h1_size>          1st hidden layer size
    #         --h2 <h2_size>          2nd hidden layer size
    #         --lr <learning_rate>    learning rate
    #         """)
    #         sys.exit()
    #     elif opt == "-l":
    #         options["log_path"] = os.path.abspath(arg)
    #     elif opt == "-g":
    #         options["meta_graph_path"] = os.path.abspath(arg)
    #     elif opt == "-e":
    #         options["epochs"] = int(arg)
    #     elif opt == "-b":
    #         options["batches"] = int(arg)
    #
    #
    #     elif opt == "-v":
    #         options["val_period"] = int(arg)
    #     elif opt == "-c":
    #         options["cp_period"] = int(arg)
    #
    #     elif opt == "--bs":
    #         options["batch_size"] = int(arg)
    #     elif opt == "--vs":
    #         options["val_size"] = int(arg)
    #     elif opt == "--ws":
    #         options["vocab_size"] = int(arg)
    #     elif opt == "--ss":
    #         options["seq_size"] = int(arg)
    #     elif opt == "--h1":
    #         options["h1_size"] = int(arg)
    #     elif opt == "--h2":
    #         options["h2_size"] = int(arg)
    #     elif opt == "--lr":
    #         options["learning_rate"] = int(arg)

    train(text_files, log_path, checkpoints_path)