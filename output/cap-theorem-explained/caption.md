Your distributed system has to give up something. The CAP theorem says which one, and when.

When your data lives on many machines, the network between them will sometimes break. CAP names the three things you want: Consistency (every read sees the latest write), Availability (every request gets an answer), and Partition tolerance (the system survives the split). You cannot guarantee all three at once. During a partition, you choose between refusing some requests to stay consistent, or answering with possibly stale data to stay available.

Follow vivektraces for more.

#systemdesign #captheorem #distributedsystems #softwareengineering #backenddevelopment #programming #computerscience #softwarearchitecture #techexplained #learntocode #developers
