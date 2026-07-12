package com.fribbels.model;

import lombok.Builder;
import lombok.Data;

@Data
@Builder
public class PassesContainer {
    int[] passBits;
    volatile boolean locked;
    String id;
}
