package com.fribbels.core;

import com.fribbels.model.Hero;
import com.fribbels.model.HeroStats;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

public class StatCalculatorTest {

    @Test
    public void pveCritDamageCapAffectsDamageButNotCp() {
        final HeroStats standardResult = calculateStats(400, false);
        final HeroStats pveResult = calculateStats(400, true);

        assertEquals(3500, standardResult.getMcdmg());
        assertEquals(4000, pveResult.getMcdmg());
        assertEquals(standardResult.getCp(), pveResult.getCp());
    }

    @Test
    public void pveCritDamageCapStopsAtFourHundredPercent() {
        final HeroStats pveResult = calculateStats(450, true);

        assertEquals(4000, pveResult.getMcdmg());
    }

    @Test
    public void pveCritDamageCapDoesNotChangeValuesBelowStandardCap() {
        final HeroStats standardResult = calculateStats(300, false);
        final HeroStats pveResult = calculateStats(300, true);

        assertEquals(standardResult.getDmg(), pveResult.getDmg());
        assertEquals(standardResult.getMcdmg(), pveResult.getMcdmg());
        assertEquals(standardResult.getCp(), pveResult.getCp());
    }

    private HeroStats calculateStats(final int critDamage, final boolean usePvECritDamageCap) {
        final HeroStats base = HeroStats.builder()
                .atk(1000)
                .hp(1000)
                .def(100)
                .spd(100)
                .cr(100)
                .cd(critDamage)
                .build();
        final Hero hero = Hero.builder().build();
        final StatCalculator calculator = new StatCalculator();
        calculator.setUsePvECritDamageCap(usePvECritDamageCap);
        calculator.setBaseValues(base, hero);

        return calculator.addAccumulatorArrsToHero(
                base,
                new float[][]{
                        new float[17], new float[17], new float[17],
                        new float[17], new float[17], new float[17]
                },
                new int[24],
                hero,
                0,
                0,
                0,
                0);
    }
}
